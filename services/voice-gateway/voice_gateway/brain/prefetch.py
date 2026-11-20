"""Speculative availability and the one-round booking turn. Owner: builder 'brain'.

Measured live (2026-11-20): a booking turn is two Bedrock rounds (the model
calls get_availability, then answers), 1.3 to 1.8 s to the first token each,
plus the tool Lambda. Two levers, both read only:

1. Prefetch (`booking_slots` + `PrefetchInvoker`): while the caller is still
   speaking, the day, the time and the number of guests are read from the
   partial transcript with the phone agent's own parsers
   (shared/features/phone_agent/readback.py: clock_times, dates, weekdays,
   party_sizes; read only). When all three are there, AgentBrain runs
   tools.Executor.get_availability on a throwaway executor (a deep copy of the
   call's toolState that is never saved) through a PrefetchInvoker: its GET
   Lambda calls (guest view, staff view, layout) are kept for the turn's own
   executor. Only GET requests are ever cached or prefetched, never a hold or
   a write (POST and every other route go straight to the Lambda). A newer
   prefetch for other slots drops the older entries; an entry is served once
   and only within PREFETCH_TTL_S, so a hold or a booking never acts on old
   availability (hold_table looks again in its own turn, and the write route
   checks the table itself).

2. One model round (`OneRoundModel`): when the caller's turn (the final
   text) asks for a booking and names day, time and number of guests, and
   nothing in the call state says the model would do something else first
   (a live hold, a found booking being moved, the language or the location
   still being settled, a disclosure the model must say), the turn's first
   model call is answered by the gateway with the get_availability call the
   prompt's step 2 asks for ("Call get_availability"), with the parsed
   arguments. agent.run_turn then runs that call through the real executor
   (every guard unchanged: the weekday and half hour checks, real
   availability, the turn bookkeeping of what was looked up when) and the
   model's first real round already has the result: it offers the times in
   one Bedrock round. A wrong parse costs nothing more than today's two
   rounds: the model sees the day and time it was given and asks again.
   Holds and writes stay the model's own calls, always.

The orchestrator's opener says "Jag kollar." only when this lookup is
planned (AgentBrain.lookup_planned): never a claim that something is being
done when it is not.
"""

from __future__ import annotations

import copy as _copy
import inspect
import json
import re
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, timedelta

from shared.agent_model import ToolUse, Turn
from shared.features.phone_agent import readback
from shared.features.phone_agent import tools as pa_tools

PREFETCH_TTL_S = 20.0            # a prefetched answer older than this is never served
WAIT_S = 3.0                     # the turn waits at most this for a prefetch still in flight
PARSED = ("sv", "en", "de", "no", "da", "nl")   # readback's word lists (zh, yue: digits only, not here)

_BOOK = re.compile(r"(?<!\w)(boka|bokning|bord|reservera|reservation|book|booking|table|reserve|tisch|reservieren|"
                   r"buchen|bestille|bestilling|bord|tafel|reserveren)(?!\w)", re.I)
_OTHER_TASK = re.compile(r"(?<!\w)(flytta|ändra|avboka|avbeställa|byta|move|change|cancel|reschedule|verschieben|"
                         r"stornieren|ändern|absagen|flytte|endre|avbestille|aflyse|ændre|verzetten|annuleren)(?!\w)",
                         re.I)
_UNDECIDED = re.compile(r"(?<!\w)(eller|or|oder|of|ellers)(?!\w)", re.I)      # "fyra eller fem": the model asks
_WE_ARE = {"sv": r"(?:vi\s+(?:är|blir)|är\s+vi|blir\s+vi)", "en": r"(?:we\s+are|we're|there\s+are|there\s+will\s+be)",
           "de": r"(?:wir\s+sind)", "no": r"(?:vi\s+er|vi\s+blir)", "da": r"(?:vi\s+er|vi\s+bliver)",
           "nl": r"(?:we\s+zijn|wij\s+zijn)"}


@dataclass(frozen=True)
class Slots:
    date: str            # YYYY-MM-DD
    time: str            # HH:MM
    party: int

    def args(self) -> dict:
        return {"date": self.date, "partySize": self.party, "time": self.time}


def _party(text: str, lang: str) -> int | None:
    found = set(readback.party_sizes(text, lang))
    low = " ".join(str(text or "").lower().split())
    pattern = _WE_ARE.get(lang)
    if pattern:
        nums = readback.NUMBERS[lang]
        alt = "|".join(re.escape(w) for w in sorted(nums, key=len, reverse=True))
        for m in re.finditer(rf"(?<!\w){pattern}\s+(?P<n>{alt}|\d{{1,2}})(?!\w)", low):
            n = m.group("n")
            found.add(int(n) if n.isdigit() else nums[n])
    found = {n for n in found if 0 < n <= 40}
    return found.pop() if len(found) == 1 else None


def _time(text: str, lang: str) -> str | None:
    """One clock time; a bare hour 1 to 10 is the evening (the prompt's rule: "seven" is 19:00)."""
    times = set(readback.clock_times(text, lang))
    if len(times) != 1:
        return None
    hour, minute = times.pop()
    if minute not in (0, 30):
        return None
    if 1 <= hour <= 10:
        hour += 12
    return f"{hour:02d}:{minute:02d}" if 11 <= hour <= 23 else None


def _date(text: str, lang: str, today: date) -> str | None:
    low = " " + " ".join(str(text or "").lower().split()) + " "
    days = {d for d in readback.dates(text, lang, None, today) if d != date.min}
    named = readback.weekdays(text, lang)
    if named:
        if len(named) != 1 or pa_tools._NEXT_WORDS.search(low) or pa_tools._WEEK_WORDS.search(low):
            return None
        ahead = (next(iter(named)) - today.weekday()) % 7
        if ahead == 0:
            return None          # today or next week: the tool asks the caller once ("i dag eller nästa fredag?")
        days.add(today + timedelta(days=ahead))
    elif pa_tools._DAY_OF_MONTH.search(low.strip()) and not days:
        return None
    if len(days) != 1:
        return None
    day = days.pop()
    return day.isoformat() if day >= today else None


def booking_intent(text: str) -> bool:
    """The words ask for a new booking (not a move or a cancellation)."""
    return bool(_BOOK.search(str(text or ""))) and not _OTHER_TASK.search(str(text or ""))


def booking_slots(text: str, lang: str | None, today: date, *, need_intent: bool = True) -> Slots | None:
    """Day, time and party of a booking request in `text`, or None when any is missing, unclear or the
    words are not a new booking (a move, a cancellation)."""
    lang = str(lang or "").split("-")[0].lower()
    if lang not in PARSED or not text:
        return None
    if need_intent and not _BOOK.search(text):
        return None
    if _OTHER_TASK.search(text) or _UNDECIDED.search(text):
        return None
    day, at, party = _date(text, lang, today), _time(text, lang), _party(text, lang)
    if not (day and at and party):
        return None
    return Slots(day, at, party)


def plain_state_allows(state: dict, call: dict | None = None) -> bool:
    """Nothing in the call says the model would do something else before looking up availability."""
    if (call or {}).get("locationPending"):
        return False
    if state.get("outcome") in ("booked", "moved", "cancelled", "transferred", "callback"):
        return False
    for key in ("found", "unsupported", "unsupportedStep", "askedSwitch", "discloseNext", "resume",
                "guaranteePending", "weekdayAsked"):
        if state.get(key):
            return False
    for proposal in (state.get("proposals") or {}).values():
        if not proposal.get("released") and not proposal.get("reservationId"):
            return False         # a live hold: the caller may be confirming or changing it
    return True


# --- the cache in front of the tool Lambdas ------------------------------------------------------------------

def _key(fn: str, event: dict) -> tuple | None:
    context = (event or {}).get("requestContext") or {}
    method = (context.get("http") or {}).get("method")
    if method != "GET":
        return None
    query = json.dumps(event.get("queryStringParameters") or {}, sort_keys=True)
    return fn, event.get("rawPath"), query


class PrefetchInvoker:
    """`invoke(fn, event)` in front of the real one: GET answers a prefetch fetched are served to the turn once,
    within PREFETCH_TTL_S; everything else passes through untouched."""

    def __init__(self, inner, now_ms=None) -> None:
        self.inner = inner
        self.now_ms = now_ms or (lambda: int(time.monotonic() * 1000))
        self._lock = threading.Lock()
        self._entries: dict[tuple, tuple[Future, float, int, int]] = {}   # key -> (answer, monotonic, at_ms, gen)
        self._gen = 0
        self.hits: list[int] = []        # age (ms on now_ms) of every prefetched answer the turn used
        self.fetched = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __call__(self, fn, event):
        key = _key(fn, event)
        if key is not None:
            with self._lock:
                entry = self._entries.pop(key, None)
            if entry is not None and time.monotonic() - entry[1] <= PREFETCH_TTL_S:
                try:
                    answer = entry[0].result(timeout=WAIT_S)
                    self.hits.append(max(0, self.now_ms() - entry[2]))
                    return answer
                except Exception:   # noqa: BLE001  (the prefetch failed: the turn asks the Lambda itself)
                    pass
        return self.inner(fn, event)

    def new_generation(self) -> int:
        """A prefetch for other slots starts: the older entries (another day or party) are dropped, the layout
        (the same for every day) is kept."""
        with self._lock:
            self._gen += 1
            self._entries = {k: v for k, v in self._entries.items() if "layout" in str(k[1])}
            return self._gen

    def prefetching(self, gen: int):
        """The invoke the throwaway executor of a prefetch uses: it fills the cache."""
        def invoke(fn, event):
            key = _key(fn, event)
            if key is None:
                raise PermissionError("a prefetch never writes")
            with self._lock:
                held = self._entries.get(key)
                if held is not None and time.monotonic() - held[1] <= PREFETCH_TTL_S:
                    future = held[0]
                    owner = False
                else:
                    future, owner = Future(), True
                    self._entries[key] = (future, time.monotonic(), self.now_ms(), gen)
            if not owner:
                return future.result(timeout=WAIT_S)
            try:
                answer = self.inner(fn, event)
            except Exception as exc:
                future.set_exception(exc)
                with self._lock:
                    if self._entries.get(key, (None,))[0] is future:
                        self._entries.pop(key, None)
                raise
            self.fetched += 1
            future.set_result(answer)
            return answer
        return invoke

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def throwaway_executor(*, call: dict, tenant_row, location, loc, invoke, today, now_time, language, text):
    """A tools.Executor on a deep copy of the call's toolState: its reads warm the cache, its state is never
    saved."""
    state = _copy.deepcopy(json.loads(call.get("toolState") or "{}")) if isinstance(call.get("toolState"), str) \
        else _copy.deepcopy(call.get("toolState") or {})
    executor = pa_tools.Executor(tenant_id=call["tenantId"], tenant_row=tenant_row, location=location,
                                 loc_config=loc, invoke=invoke, state=state, caller_phone=call.get("from"),
                                 call_ref=call.get("callRef"), today=today, now_time=now_time, language=language)
    executor.caller_text = text
    return executor


# --- the one-round model -------------------------------------------------------------------------------------

class OneRoundModel:
    """The turn's model: the first converse call is answered with `planned` (a get_availability tool use) when
    set, every later call (and every call without a plan) goes to the real model. `real_rounds` counts the
    calls that reached it."""

    def __init__(self, inner, planned: Slots | None) -> None:
        self.inner, self.planned = inner, planned
        self.real_rounds = 0
        self.answered_first = False

    def converse(self, system, messages, tools, *, max_tokens, timeout_s=None):
        planned, self.planned = self.planned, None
        if planned is not None and any((t.get("toolSpec") or t).get("name") == "get_availability"
                                       for t in tools or ()):
            self.answered_first = True
            use = ToolUse(f"tooluse_gw{uuid.uuid4().hex[:20]}", "get_availability", planned.args())
            return Turn(None, (use,), "tool_use")
        self.real_rounds += 1
        extra = {"timeout_s": timeout_s} if _takes_timeout(self.inner.converse) else {}
        return self.inner.converse(system, messages, tools, max_tokens=max_tokens, **extra)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _takes_timeout(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "timeout_s" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())
