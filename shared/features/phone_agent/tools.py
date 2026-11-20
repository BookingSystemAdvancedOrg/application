"""The agent's tools (ADR-L "Bedrock agent: tools and the Lambdas behind
them"). Every tool runs scoped to the call's tenant and location; the
model cannot name another location. Existing Lambdas are invoked on their
`live` alias with a synthesized HTTP API v2 event (the agent principal:
claims `{tenant_id, role: staff_user, sub: agent:<l>}`); nothing here reads
or writes their tables. Results are summarised for the model (no raw rows,
no other guests' names or numbers).

`invoke(function_name, event) -> (status, body)` is injected: the Lambda
client in production (`lambda_invoker`), a fake in tests. `dry_run` (the
wizard's simulator, route L6) runs the reads and answers every write
without writing anything.
"""

from __future__ import annotations

import copy as _copy
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from shared import agent_model, availability as shared_availability, booking_rules, guarantee, messages
from shared.features import phone_agent as pa
from shared.features.phone_agent import copy
from shared.features.phone_agent import offers as of
from shared.features.phone_agent import readback

_DATE = {"type": "string", "description": "YYYY-MM-DD, from the calendar in your instructions"}
_TIME = {"type": "string", "description": "HH:MM, 24 hour clock, on the hour or half hour"}
_PARTY = {"type": "integer", "minimum": 1, "description": "number of guests"}
_CONFIRMED = {"type": "boolean", "description": "true only after the caller said a clear yes to your read-back"}
TOOLS = [
    {"name": "get_restaurant_info", "description": "Address, opening hours, special days and booking rules.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_availability",
     "description": "Free start times for a party on one day. `suggest` holds the (at most three) free times "
                    "nearest the wished time: offer those. A full day lists `nextDays` with free times. A result "
                    "with `code` and `say` (pastDate, beyondHorizon, largeParty, guaranteeRequired): follow `say`. "
                    "`guaranteeRequired` with times and a `say`: offer the times, and follow `say` (the fee rule "
                    "in `guaranteeTerms` before the read-back, amounts only if the caller asks).",
     "input_schema": {"type": "object", "required": ["date", "partySize"], "properties": {
         "date": _DATE, "partySize": _PARTY, "time": _TIME, "timeFrom": _TIME, "timeTo": _TIME}}},
    {"name": "hold_table",
     "description": "Check that a table can be booked at the one time the caller chose; writes nothing. Call it "
                    "once per reply: a new hold replaces the previous one, and only the latest hold can be "
                    "booked (to compare times use get_availability). Returns `holdId`, or other times "
                    "(`nearby` first if present, else exactly the times in `suggest`), or `code` and `say`. "
                    "`guaranteeRequired` with a holdId: first say `guaranteeTerms.sentence` and ask "
                    "`guaranteeTerms.question` (no amounts unless the caller asks), then the read-back also says "
                    "the card link is texted.",
     "input_schema": {"type": "object", "required": ["date", "time", "partySize"], "properties": {
         "date": _DATE, "time": _TIME, "partySize": _PARTY,
         "tableIds": {"type": "array", "items": {"type": "string"},
                      "description": "only the tableIds of a `nearby` option the caller accepted"}}}},
    {"name": "create_reservation",
     "description": "Book a held table, only after the caller said a clear yes to your read-back of day, time, "
                    "number of guests and name. When the party needs a card guarantee it texts the caller a link "
                    "to add the card and answers `guaranteeLinkSent` with the `sentence` to say: the table is "
                    "then held, not booked, until the card is added.",
     "input_schema": {"type": "object", "required": ["holdId", "name", "confirmed"], "properties": {
         "holdId": {"type": "string"}, "name": {"type": "string", "description": "the name as the caller gave it"},
         "confirmed": _CONFIRMED,
         "phone": {"type": "string", "description": "only when the caller's number is hidden: the number they "
                                                   "gave and confirmed"},
         "note": {"type": "string", "description": "a short note for the restaurant, for example an allergy"}}}},
    {"name": "send_guarantee_link",
     "description": "Send the card guarantee text message again, once, only when the caller says the text "
                    "from create_reservation did not arrive. Never ask for card details.",
     "input_schema": {"type": "object", "required": ["reservationId"],
                      "properties": {"reservationId": {"type": "string"}}}},
    {"name": "find_reservations", "description": "The caller's upcoming bookings, found by the number they call "
                                                 "from. A hidden number finds none.",
     "input_schema": {"type": "object", "properties": {"fromDate": _DATE}}},
    {"name": "move_reservation",
     "description": "Move a booking found by find_reservations, only after a read-back and a clear yes. A taken "
                    "time returns other times instead.",
     "input_schema": {"type": "object", "required": ["reservationId", "date", "time", "confirmed"], "properties": {
         "reservationId": {"type": "string"}, "date": _DATE, "time": _TIME, "partySize": _PARTY,
         "confirmed": _CONFIRMED}}},
    {"name": "cancel_reservation",
     "description": "Cancel a booking found by find_reservations, only after a read-back and a clear yes.",
     "input_schema": {"type": "object", "required": ["reservationId", "confirmed"], "properties": {
         "reservationId": {"type": "string"}, "reason": {"type": "string"}, "confirmed": _CONFIRMED}}},
    {"name": "get_menu", "description": "Dishes on today's menu matching a question, with prices. A dish that is "
                                        "not listed is not available today. The menu is stored in Swedish: say "
                                        "dish names exactly as returned.",
     "input_schema": {"type": "object", "properties": {"query": {
         "type": "string", "description": "search words in Swedish (translate the caller's words, for example "
                                          "chicken: kyckling, fish: fisk)"}}}},
    {"name": "transfer_to_staff", "description": "Transfer the caller to a person at the restaurant.",
     "input_schema": {"type": "object", "required": ["reason"], "properties": {"reason": {"type": "string"}}}},
    {"name": "request_callback",
     "description": "Ask the restaurant to call the caller back. topic starts with booking, largeParty, guarantee, "
                    "language:<code> or other, then a few words; never a transcript.",
     "input_schema": {"type": "object", "required": ["topic"], "properties": {
         "name": {"type": "string"}, "phone": {"type": "string"}, "topic": {"type": "string"},
         "preferredTime": {"type": "string"}}}},
    {"name": "end_call", "description": "End the call after saying goodbye.",
     "input_schema": {"type": "object", "required": ["outcome", "summary"], "properties": {
         "outcome": {"type": "string", "enum": ["booked", "moved", "cancelled", "info", "transferred", "callback",
                                                "abandoned"]},
         "summary": {"type": "string", "description": "a few words, no names or numbers"}}}},
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)
FIND_POOL = 7
NEXT_DAYS = 4            # days checked after a full day (in parallel)
NEXT_DAYS_SHOWN = 2
MAX_TIMES = 12
_E164 = re.compile(r"\+[1-9]\d{7,14}")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DAY_KEYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6, "monday": 0, "tuesday": 1,
             "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
CALLBACK_TOPICS = ("booking", "largeParty", "guarantee", "language:", "other")
# The numbers an SMS goes to (the notification Lambda's rule, SMS_ALLOWED_PREFIXES): Nordic only.
SMS_PREFIXES = ("+46", "+45", "+47", "+358", "+354")
# What the model is told to say (English; it speaks the caller's language).
SAY = {
    "pastDate": "That day has already passed. Ask politely which day the caller means.",
    "beyondHorizon": ("Bookings open {days} days ahead, until {lastDate}. Offer the last bookable day or that "
                      "the restaurant calls back."),
    "largeParty": ("A party of this size is booked by the restaurant itself. Say: \"" + copy.LARGE_PARTY["en"] +
                   "\" If yes, call request_callback with topic largeParty."),
    "guaranteeRequired": ("This booking needs a card guarantee, which cannot be taken by phone. Say: \"" +
                          copy.GUARANTEE_CALLBACK["en"] + "\" Do not read back or book. If yes, call "
                          "request_callback with topic guarantee."),
    # The card is added on a Stripe page the caller gets by text message (create_reservation sends it).
    "guaranteeLink": ("This party needs a card guarantee; `guaranteeTerms` holds this restaurant's real terms for "
                      "this party. Offer the times as usual. Before the read-back, in a reply of its own, say "
                      "`guaranteeTerms.sentence` in the caller's language: a card is needed, nothing is charged "
                      "now, and when a fee is charged (only the fees and the cutoff it names), with no amount. In "
                      "Swedish it starts \"För den bokningen behövs ett kort som garanti\". End that reply with "
                      "`guaranteeTerms.question` (whether the caller wants more information). Only if the caller "
                      "wants more information, say `guaranteeTerms.ifAsked.details` (the amounts as in its readAs) "
                      "and answer further questions from `guaranteeTerms.ifAsked`: the amounts per person, free "
                      "cancellation until the cutoff through the link in the text message or by calling the "
                      "restaurant, the card is handled securely by Stripe, nothing is charged if they come. "
                      "Otherwise never say an amount. Never say an amount, a fee or a cutoff that is not in "
                      "`guaranteeTerms`. Then the read-back: day, time, number of guests and name, that a text "
                      "message with a link to add a card as a guarantee will be sent to the number the caller is "
                      "calling from, and that the booking is confirmed once the card is added; in Swedish end it "
                      "with \"Stämmer det?\". The answer to the information question is never a yes to the "
                      "read-back. Never ask for card details."),
    # The same without terms (the location's policy could not be read): no amounts at all.
    "guaranteeLinkNoTerms": ("This party needs a card guarantee. Offer the times as usual. Before the read-back say "
                             "that the booking needs a card as a guarantee and that nothing is charged now (in "
                             "Swedish: \"För den bokningen behövs ett kort som garanti, men inget dras nu.\"); "
                             "never name an amount, a fee or a cutoff: the restaurant's terms are in the text "
                             "message. In the read-back also say that a text message with a link to "
                             "add a card as a guarantee will be sent to the number the caller is calling from, and "
                             "that the booking is confirmed once the card is added; in Swedish end it with "
                             "\"Stämmer det?\". Never ask for card details."),
    "guaranteeLinkSent": ("The table is held, not booked, until the caller adds the card through the text "
                          "message. Say the sentence in `sentence` word for word, in the caller's language, "
                          "instead of a booking confirmation. Then ask whether you can help with anything else."),
    "guaranteeLinkFailed": ("The text message with the card link could not be sent. Apologise, say: \"" +
                            copy.GUARANTEE_CALLBACK["en"] + "\" If yes, call request_callback with topic "
                            "guarantee."),
    "notConfirmed": "Read back the day, time, number of guests and name, and wait for a clear yes first.",
    # The caller has not yet heard a read-back of this: the table, the booking or the new day was only
    # looked up in this reply (a change, or a yes to something else). The check stays valid.
    "readBackFirst": ("The caller has not heard a read-back of this yet. Read it back now in one sentence and "
                      "ask whether it is correct; after a clear yes, call the tool again."),
    "alreadyBooked": ("This call already booked a table on that day (see booking). Do not book again. To change "
                      "it, read back the change and after a clear yes call move_reservation with its "
                      "reservationId; for a second table that day, offer that the restaurant calls back."),
    "nameInvalid": "The name could not be used. Ask for the name again.",
    # Live test 2026-11-20: several holds in one reply, the read-back of one, the booking of another.
    "holdReplaced": ("Not booked: that hold was replaced by a later one. Only `live` can be booked. If the "
                     "caller said yes to a read-back of `live` (its day, time and number of guests), call "
                     "create_reservation with its holdId; otherwise read `live` back and wait for a clear yes."),
    "readBackMismatch": ("Not booked: your last reply did not read back this hold's day and time (`hold`). "
                         "Read back exactly that day, time, number of guests and name in one sentence and "
                         "ask whether it is correct; after a clear yes, call create_reservation again."),
    "termsFirst": ("Not booked yet: the caller has not heard the card guarantee's terms. Say "
                   "`guaranteeTerms.sentence` now and end with `guaranteeTerms.question` (no amounts unless "
                   "asked). Then read back the booking, and after a clear yes call create_reservation again."),
    "phoneInvalid": "The phone number could not be used. Ask for it again, or book without a number.",
}
# Beside a time lookup once the caller's own booking was found: a move is not a new booking.
MOVE_NEXT = ("If the caller wants to move the booking found by find_reservations: ask for no name and do not "
             "hold. When they pick a time, read back the old and the new day and time, ask whether to move it, "
             "and after a clear yes call move_reservation.")
# The existing Lambdas the tools invoke (`<env>-<name>:live`). A cancel goes
# through the staff status route on mark-arrived, never cancel-reservation
# (a guest route that needs the manage-link token).
FUNCTIONS = ("tenant-site-config", "get-availability", "create-pending-reservation", "get-reservation",
             "mark-arrived", "get-menu", "list-layout-version")


def function_name(name) -> str:
    env = os.environ.get("ENVIRONMENT", "dev")
    return f"{'' if env == 'prod' else env + '-'}{name}:live"


def http_event(method, template, params, *, query=None, body=None, claims=None):
    path = template
    for k, v in params.items():
        path = path.replace("{" + k + "}", v)
    context = {"http": {"method": method, "path": path, "sourceIp": "127.0.0.1"}}
    if claims:
        context["authorizer"] = {"jwt": {"claims": claims}}
    return {"version": "2.0", "routeKey": f"{method} {template}", "rawPath": path, "pathParameters": params,
            "queryStringParameters": query, "headers": {"content-type": "application/json"},
            "requestContext": context, "body": json.dumps(body) if body is not None else None,
            "isBase64Encoded": False}


def lambda_invoker(client):
    """invoke(name, event) on `<prefix><name>:live` with the Lambda client."""
    def invoke(name, event):
        answer = client.invoke(FunctionName=function_name(name), InvocationType="RequestResponse",
                               Payload=json.dumps(event).encode("utf-8"))
        if answer.get("FunctionError"):
            raise pa.PhoneAgentError(502, "tool_unavailable")
        out = json.loads(answer["Payload"].read() or b"{}")
        body = out.get("body")
        try:
            body = json.loads(body) if isinstance(body, str) and body else (body or {})
        except ValueError:
            body = {}
        return int(out.get("statusCode") or 500), body
    return invoke


def _past_midnight(hours):
    """True when a weekday closes after midnight (closesAt before opensAt, or 00:00)."""
    if not isinstance(hours, dict):
        return False
    return any(isinstance(i, dict) and isinstance(i.get("opensAt"), str) and isinstance(i.get("closesAt"), str)
               and (i["closesAt"] < i["opensAt"] or i["closesAt"] == "00:00")
               for day in hours.values() if isinstance(day, list) for i in day)




def _special_day(day):
    """A special day as the site-config answers it ({date, kind, intervals?, note?})."""
    if not isinstance(day, dict) or not day.get("date"):
        return None
    kind = day.get("kind")
    out = {"date": day["date"], "weekday": _weekday(day["date"]),
           "kind": {"closed": "closed", "hours": "special hours", "no_online": "no online booking"}.get(
               kind, str(kind or ""))}
    if kind == "hours":
        out["hours"] = [{"open": i.get("opensAt"), "close": i.get("closesAt")} for i in day.get("intervals") or []]
    if day.get("note"):
        out["note"] = " ".join(str(day["note"]).split())[:80]
    return out


def _weekday(value):
    try:
        return _WEEKDAYS[date.fromisoformat(str(value)).weekday()]
    except ValueError:
        return None


def _hours(hours):
    """Opening hours as [{weekday, open, close}] in week order."""
    out = []
    if not isinstance(hours, dict):
        return out
    for key, spans in sorted(hours.items(), key=lambda kv: _DAY_KEYS.get(str(kv[0]).lower(), 9)):
        index = _DAY_KEYS.get(str(key).lower())
        if index is None or not isinstance(spans, list):
            continue
        for span in spans:
            if isinstance(span, dict) and span.get("opensAt") and span.get("closesAt"):
                out.append({"weekday": _WEEKDAYS[index], "open": span["opensAt"], "close": span["closesAt"]})
    return out


def _say(code, **fields):
    return {"code": code, "say": SAY[code].format(**fields)}


class Executor:
    """Runs one tool use for one call. `state` is the call's (or simulator
    session's) persisted tool state: proposals, the first request, the
    furthest counter-offer, and what the call achieved."""

    def __init__(self, *, tenant_id, tenant_row, location, loc_config, invoke, state, caller_phone=None, dry_run=False,
                 voice=None, call_ref=None, callbacks=None, today=None, language="sv", now_time=None,
                 send_sms=None):
        self.tenant_id = tenant_id
        self.tenant_row = tenant_row
        self.location = location
        self.location_id = location["locationId"]
        self.loc = loc_config
        self.invoke = invoke
        self.state = state
        self.state.setdefault("proposals", {})
        self.caller_phone = caller_phone
        self.dry_run = dry_run
        self.voice = voice
        self.call_ref = call_ref
        self.callbacks = callbacks
        # send_sms(tenant_id=, location_id=, to=, text=): one SMS to the caller (the notification
        # Lambda's feature notice); None: no SMS possible, a card guarantee becomes a callback.
        self.send_sms = send_sms
        self.today = today or date.today()
        self.now_time = now_time      # "HH:MM" local: today's started times are never offered (staff view)
        self.last_day = shared_availability.last_bookable_date(self.today, location)
        self.language = language or "sv"
        # The caller utterance this runs in (session.call_turn / sim_turn count them). A write needs the
        # thing it writes to have been looked up in an EARLIER utterance: the caller cannot have said yes
        # to a read-back of something first looked up in this one. None (a direct call in tests): no check.
        self.turn = state.get("turn")
        self._cache = {}

    # -- plumbing ---------------------------------------------------------------------

    @property
    def claims(self):
        return {"tenant_id": self.tenant_id, "role": "staff_user", "sub": pa.AGENT_USER_PREFIX + self.location_id,
                "cognito:groups": ["staff_user"]}

    @property
    def caller_known(self) -> bool:
        """The network gave a usable caller ID (E.164)."""
        return isinstance(self.caller_phone, str) and bool(_E164.fullmatch(self.caller_phone))

    @property
    def online_max(self):
        value = (self.location or {}).get("maxPartySizeOnline")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def phone_max(self) -> int:
        """The largest party the agent books itself (phoneMaxPartySize, default
        the online maximum); above it the restaurant calls back."""
        own = self.loc.get("phoneMaxPartySize")
        if own is not None:
            return int(own)
        return self.online_max if self.online_max is not None else booking_rules.MAX_PARTY

    def _call(self, fn, method, template, params=None, *, query=None, body=None, staff=False):
        params = {"locationId": self.location_id, **(params or {})}
        key = (fn, method, template, json.dumps(params, sort_keys=True), json.dumps(query, sort_keys=True))
        if method == "GET" and key in self._cache:
            return self._cache[key]
        answer = self.invoke(fn, http_event(method, template, params, query=query, body=body,
                                            claims=self.claims if staff else None))
        if method == "GET":
            self._cache[key] = answer
        return answer

    def run(self, name, args):
        if name not in TOOL_NAMES:
            return {"ok": False, "code": "unknown_tool"}
        try:
            return getattr(self, name)(dict(args or {}))
        except pa.PhoneAgentError as exc:
            return {"ok": False, "code": exc.code}
        except (TypeError, ValueError, KeyError):
            return {"ok": False, "code": "invalid_arguments"}

    # -- the turn deadline (agent.run_turn) ---------------------------------------------

    def fork(self):
        """A copy for a read the turn may stop waiting for: a read still
        running after the deadline changes only its fork, never the state
        saved with the call."""
        twin = _copy.copy(self)
        twin.state = _copy.deepcopy(self.state)
        twin._cache = dict(self._cache)
        return twin

    def join(self, twin):
        """A fork's read finished in time: its state is the call's."""
        self.state.clear()
        self.state.update(twin.state)
        self._cache.update(twin._cache)

    def not_heard(self, reply=None, lang=None):
        """The reply of this utterance was cut by the deadline (the caller
        heard only that the agent is still checking): what it looked up
        counts as looked up in the next utterance, so a write still needs a
        read-back the caller hears first, and the next turn resumes. When the
        sentence said (`reply`, the model's own announcement) names another
        day, time or party than the live hold, its earlier read-back is void."""
        _, live = self._live()
        if reply and live and readback.covers(reply, live["date"], live["time"], lang or self.language,
                                              party=live.get("partySize"), today=self.today) is False:
            self.state.pop("readBack", None)
        self.state["resume"] = True
        self.state["heard"] = self.turn       # nothing of this utterance was read back
        if self.turn is None:
            return
        now, later = int(self.turn), int(self.turn) + 1
        for proposal in self.state["proposals"].values():
            if proposal.get("turn") is not None and int(proposal["turn"]) >= now:
                proposal["turn"] = later
        turns = self.state.get("foundTurn") or {}
        for rid, seen in list(turns.items()):
            if int(seen) >= now:
                turns[rid] = later

    # -- checks before any read ---------------------------------------------------------

    @staticmethod
    def _day(value):
        return date.fromisoformat(str(value)[:10]).isoformat()

    @staticmethod
    def _time(value):
        """"HH:MM" on the slot grid; "19.15" or "1915" are read too, a time
        between two slots is moved to the earlier one (the tool's suggest
        then lists the nearest)."""
        digits = re.sub(r"\D", "", str(value))
        if len(digits) in (3, 4):
            hour, minute = int(digits[:-2]), int(digits[-2:])
        elif len(digits) in (1, 2):
            hour, minute = int(digits), 0
        else:
            raise ValueError("time")
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("time")
        step = shared_availability.SLOT_STEP_MINUTES
        minute -= minute % step
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def _party(value):
        party = int(value)
        if party < 1 or party > booking_rules.MAX_PARTY * 4:
            raise ValueError("partySize")
        return party

    def _heard_before(self, seen_turn):
        """The thing a write acts on was looked up in an earlier caller utterance than this one."""
        return self.turn is None or (seen_turn is not None and int(seen_turn) < int(self.turn))

    def _date_problem(self, day):
        when = date.fromisoformat(day)
        if when < self.today:
            return {"date": day, **_say("pastDate")}
        if when > self.last_day:
            return {"date": day, "lastDate": self.last_day.isoformat(),
                    **_say("beyondHorizon", days=shared_availability.BOOKING_HORIZON_DAYS,
                           lastDate=self.last_day.isoformat())}
        return None

    @property
    def sms_reachable(self) -> bool:
        """The caller ID is a number an SMS goes to (Nordic, as the notification Lambda sends)."""
        raw = os.environ.get("SMS_ALLOWED_PREFIXES") or ",".join(SMS_PREFIXES)
        prefixes = tuple(p.strip() for p in raw.split(",") if p.strip().startswith("+"))
        return self.caller_known and self.caller_phone.startswith(prefixes)

    def _guarantee_mode(self, party):
        """How a card guarantee the location asks for this party is taken by
        phone: None (no card: none required, or phoneGuarantee "skip"),
        "link" (create_reservation books it pending and texts the caller a
        Stripe link) or "callback" (hidden or foreign number, no SMS: the
        restaurant calls back)."""
        if not guarantee.required(self.location, self.tenant_row, party):
            return None
        if pa.phone_guarantee(self.loc) == "skip":
            return None
        if self.dry_run or (self.send_sms is not None and self.sms_reachable):
            return "link"
        return "callback"

    def _party_problem(self, party, *, moving=False):
        """`moving`: a booking's party changed by move_reservation, which
        edits it in place (no pending state, no link, no new fee snapshot):
        any party the card guarantee covers is the restaurant's callback."""
        if party > self.phone_max:
            self.state["needsCallback"] = "largeParty"
            return {"partySize": party, "largeParty": True, **_say("largeParty")}
        if self._guarantee_mode(party) in (("link", "callback") if moving else ("callback",)):
            return self._guarantee_callback(party)
        return None

    def _guarantee_callback(self, party):
        self.state["needsCallback"] = "guarantee"
        return {"partySize": party, "guaranteeRequired": True, **_say("guaranteeRequired")}

    def _guarantee_note(self, party, *, new_booking=False):
        """Beside times and a hold when the card link will be texted at
        booking; not while the caller's own bookings are found (a move never
        texts a link), unless `new_booking` (create_reservation: the terms a
        new booking after a lookup still needs said first)."""
        if (new_booking or not self.state.get("found")) and self._guarantee_mode(party) == "link":
            terms = self._guarantee_terms(party)
            if not terms:
                return {"guaranteeRequired": True, "say": SAY["guaranteeLinkNoTerms"]}
            return {"guaranteeRequired": True, "guaranteeTerms": terms, "say": SAY["guaranteeLink"]}
        return {}

    def _guarantee_terms(self, party):
        """The card guarantee's terms for this party, computed as the booking
        will snapshot them (shared/guarantee.snapshot). By default the model
        says `sentence` (a card is needed, nothing is charged now, and when a
        fee is charged: only the fees this restaurant charges, its own
        cutoff, no amount) and asks `question`; `ifAsked` holds the totals
        and per-person amounts in kronor with their spoken English form
        (readAs) and `details`, the amounts sentence, said only when the
        caller wants more information. Sentences in the caller's language
        (Swedish or English; others translate the English one). A fee that
        is 0 is left out, a late fee without a cutoff too; None without a
        policy."""
        if not guarantee.policy_of(self.location):
            return None
        snap = guarantee.snapshot(self.location, party)
        no_show, late = int(snap["noShowFee"]) // 100, int(snap["lateCancelFee"]) // 100
        hours = int(snap["cancelCutoffHours"])
        late_said = bool(late and hours)     # no cutoff: cancelling is never late, the fee is not said
        fees = [x for x, on in (("noShow", no_show), ("lateCancel", late_said)) if on]
        terms = {"chargedNow": 0, "fees": fees}
        if late_said:
            terms.update({"cancelCutoffHours": hours, "readAs": {"cancelCutoffHours": hours_words(hours)}})
        details = {"currency": "SEK"}
        said = {"chargedNow": kronor_words(0)}
        if no_show:
            details.update({"noShowFee": no_show, "noShowFeePerPerson": int(snap["noShowFeePerPerson"])})
            said.update({"noShowFee": kronor_words(no_show),
                         "noShowFeePerPerson": kronor_words(snap["noShowFeePerPerson"]) + " per person"})
        if late_said:
            details.update({"lateCancelFee": late, "lateCancelFeePerPerson": int(snap["lateCancelFeePerPerson"])})
            said.update({"lateCancelFee": kronor_words(late),
                         "lateCancelFeePerPerson": kronor_words(snap["lateCancelFeePerPerson"]) + " per person"})
        lang = "sv" if self.language == "sv" else "en"
        words = copy.guarantee_terms(
            lang, no_show=kronor_words(no_show, lang) if no_show else None,
            late=kronor_words(late, lang) if late_said else None,
            hours=hours_words(hours, lang) if late_said else None)
        terms["sentence"] = words["sentence"]
        if words["question"]:
            terms["question"] = words["question"]
            terms["ifAsked"] = {**details, "details": words["details"], "readAs": said}
        return terms

    # -- reads ------------------------------------------------------------------------

    def _staff_view(self, day):
        s, staff = self._call("get-availability", "GET", "/locations/{locationId}/availability", query={"date": day})
        return staff if s == 200 else {}

    def _layout(self):
        s, layout = self._call("list-layout-version", "GET", "/locations/{locationId}/layout/active")
        return layout if s == 200 else {}

    def _staff_path(self, party):
        """Above the online maximum but within phoneMaxPartySize: the staff view."""
        return self.online_max is not None and party > self.online_max

    def _upcoming(self, day, staff):
        """The staff view lists times that already started today (walk-ins): not for a phone booking."""
        if not self.now_time or day != self.today.isoformat() or not isinstance(staff, dict):
            return staff
        return {**staff, "slots": [s for s in staff.get("slots") or [] if str(s.get("startTime") or "") >
                                   self.now_time]}

    def _views(self, day, party):
        if self._staff_path(party):
            return None, self._upcoming(day, self._staff_view(day)), self._layout()
        status, guest = self._call("get-availability", "GET", "/locations/{locationId}/availability",
                                   query={"date": day, "partySize": str(party)})
        if status != 200:
            raise pa.PhoneAgentError(502, "availability_unavailable")
        offers = self.loc.get("counterOffers") or {}
        staff, layout = {}, {}
        if offers.get("nearbyTables", True):
            staff, layout = self._staff_view(day), self._layout()
        return guest, staff, layout

    def _offers(self, day, time, party):
        guest, staff, layout = self._views(day, party)
        if guest is None:
            return of.staff_offers(staff, layout, time, party)
        offers = self.loc.get("counterOffers") or {}
        return of.offers(guest, staff, layout, time, party, nearby_tables=offers.get("nearbyTables", True),
                         phone_only_tables=offers.get("phoneOnlyTables", True))

    def _answer(self, day, party, time=None):
        guest, staff, layout = self._views(day, party)
        if guest is None:
            times = of.staff_times(staff, layout, party)
            answer = {"times": times, "before": [], "after": [], "nearby": [], "dayFull": not times}
            if time:
                answer["requestedTime"] = time
                answer["before"] = [t for t in times if t < time][::-1]
                answer["after"] = [t for t in times if t > time]
            return of.requested_first(answer, time)
        offers = self.loc.get("counterOffers") or {}
        return of.availability_answer(guest, staff, layout, party, time,
                                      nearby_tables=offers.get("nearbyTables", True),
                                      phone_only_tables=offers.get("phoneOnlyTables", True))

    def _note_request(self, day, time):
        first = self.state.setdefault("first", {"date": day, "time": time})
        if time and not first.get("time"):
            first["time"] = time
        return first

    def _reached(self, step):
        order = pa.COUNTER_OFFERS
        if order.index(step) > order.index(self.state.get("furthest") or "none"):
            self.state["furthest"] = step

    def get_restaurant_info(self, args):
        """This location's address, opening hours, special days and booking
        rules, and nothing about any other location of the tenant."""
        status, body = self._call("tenant-site-config", "GET", "/site-config", {},
                                  query={"tenantId": self.tenant_id})
        if status != 200:
            return {"ok": False, "code": "info_unavailable"}
        loc = next((x for x in body.get("locations") or [] if x.get("locationId") == self.location_id), None)
        if not loc:
            return {"ok": False, "code": "info_unavailable"}
        out = {"name": loc.get("name")}
        address = loc.get("address")
        if isinstance(address, dict):
            address = ", ".join(str(address[k]) for k in ("street", "postalCode", "city") if address.get(k))
        if address:
            out["address"] = address
        hours = loc.get("businessHours") or loc.get("openingHours")
        if hours:
            out["hours"] = _hours(hours)
            if _past_midnight(hours):
                out["hoursNote"] = "A closing time before the opening time means open past midnight."
        special = [x for x in (_special_day(d) for d in (loc.get("specialDays") or [])[:6]) if x]
        if special:
            out["specialDays"] = special
        if loc.get("maxPartySizeOnline") is not None:
            out["largestPartyByPhone"] = self.phone_max
        if loc.get("bookingDurationHours") is not None:
            out["bookingLengthMinutes"] = int(float(loc["bookingDurationHours"]) * 60)
        out["lastBookableDate"] = self.last_day.isoformat()
        out["sourceLanguage"] = "sv"      # names as stored; descriptions translated by the model
        return out

    def _next_days(self, day, party, time):
        """The next bookable days after a full one, checked in parallel; the
        first NEXT_DAYS_SHOWN with free times and their nearest times."""
        start = date.fromisoformat(day)
        days = [(start + timedelta(days=i)).isoformat() for i in range(1, NEXT_DAYS + 1)
                if start + timedelta(days=i) <= self.last_day]
        if not days:
            return []

        def one(d):
            try:
                return d, self._answer(d, party)["times"]
            except pa.PhoneAgentError:
                return d, []

        with ThreadPoolExecutor(min(NEXT_DAYS, len(days))) as pool:
            found = list(pool.map(one, days))
        out = []
        for d, times in found:
            if times:
                near = agent_model.nearest_times(time, times, (), limit=2) if time else times[:2]
                out.append({"date": d, "weekday": _weekday(d), "suggest": near})
            if len(out) == NEXT_DAYS_SHOWN:
                break
        return out

    def get_availability(self, args):
        day, party = self._day(args["date"]), self._party(args["partySize"])
        time = self._time(args["time"]) if args.get("time") else None
        problem = self._date_problem(day) or self._party_problem(party)
        if problem:
            return {"date": day, "weekday": _weekday(day), "times": [], **problem}
        first = self._note_request(day, time)
        if day != first["date"]:
            self._reached("next_day")
        answer = self._answer(day, party, time)
        frm = self._time(args["timeFrom"]) if args.get("timeFrom") else None
        to = self._time(args["timeTo"]) if args.get("timeTo") else None
        if frm or to:
            answer["times"] = [t for t in answer["times"] if (not frm or t >= frm) and (not to or t <= to)]
        if time and answer["times"]:
            answer["suggest"] = agent_model.nearest_times(time, answer["times"], ())
        answer["before"], answer["after"] = answer["before"][:3], answer["after"][:3]
        answer["times"] = of.spread(answer["times"], time, MAX_TIMES)
        answer.update({"date": day, "weekday": _weekday(day), "partySize": party, **self._guarantee_note(party)})
        if self.state.get("found"):
            answer["next"] = MOVE_NEXT
        if not answer["times"] and not answer.get("nearby"):
            answer["dayFull"] = True
            answer["nextDays"] = self._next_days(day, party, time)
            if answer["nextDays"]:
                self._reached("next_day")
        return answer

    def hold_table(self, args):
        """A new hold, a failed one or another time offered: the read-back of
        the earlier live hold no longer counts (the caller asked for something
        else since). The same hold checked again keeps it."""
        before = (self.state.get("readBack") or {}).get("holdId")
        result = self._hold_table(args)
        if before is not None and result.get("holdId") != before:
            self.state.pop("readBack", None)
        return result

    def _hold_table(self, args):
        day, time, party = self._day(args["date"]), self._time(args["time"]), self._party(args["partySize"])
        problem = self._date_problem(day) or self._party_problem(party)
        if problem:
            return {"held": False, "date": day, "time": time, **problem}
        first = self._note_request(day, time)
        if day != first["date"]:
            self._reached("next_day")
        wanted = [str(t) for t in args.get("tableIds") or []]
        result = self._offers(day, time, party)
        kind = None
        if wanted:  # the nearby table the caller said yes to: must still be one of today's nearby options
            match = next((n for n in result.get("nearby") or [] if sorted(n["tableIds"]) == sorted(wanted)), None)
            if result.get("bookable") and sorted(result["tableIds"]) == sorted(wanted):
                match = {"tableIds": wanted, "kind": None}
            if not match:
                return {**self._counter(result, time), "reason": "time_taken"}
            table_ids, kind = match["tableIds"], match.get("kind")
        elif result.get("bookable"):
            table_ids = result["tableIds"]
        else:
            return self._counter(result, time)
        # The same table again (the model re-checks before booking): the read-back of the first check holds.
        # A replaced hold checked again is live again under its own id (its read-back still counts only
        # if no later reply contradicted it: heard).
        same = next((k for k, p in reversed(list(self.state["proposals"].items())) if not p.get("reservationId")
                     and (p["date"], p["time"], int(p["partySize"]), sorted(p["tableIds"])) ==
                     (day, time, party, sorted(table_ids))), None)
        hold_id = same or f"p{len(self.state['proposals']) + 1}"
        if same:
            self.state["proposals"][same].pop("released", None)
            # Last in the order: _live() answers the latest hold.
            self.state["proposals"][same] = self.state["proposals"].pop(same)
        if same and self.state["proposals"][same].get("turn") is None:
            self.state["proposals"][same]["turn"] = self.turn
        if not same:
            self.state["proposals"][hold_id] = {"date": day, "time": time, "partySize": party,
                                                "tableIds": table_ids, "nearby": bool(kind), "turn": self.turn}
        replaced = self._release_others(hold_id)
        note = self._guarantee_note(party)
        if note:     # the caller must hear the card terms before this hold's read-back (heard)
            proposal = self.state["proposals"][hold_id]
            proposal["guaranteeDue"] = "fee" if (note.get("guaranteeTerms") or {}).get("fees") else "card"
            proposal["guaranteeNoted"] = True      # an executor that is never told what was said (tests)
        held = {"holdId": hold_id, "date": day, "weekday": _weekday(day), "time": time, "partySize": party,
                **note}
        if replaced:
            held["replaced"] = replaced
            held["note"] = ("This hold replaced " + ", ".join(replaced) + ": only " + hold_id + " can be booked. "
                            "Hold only the one time the caller chose.")
        if self.state.get("found"):
            held["next"] = MOVE_NEXT
        return held

    def _live(self):
        """(holdId, proposal) of the one hold that can be booked, else (None, None)."""
        live = [(k, p) for k, p in self.state["proposals"].items()
                if not p.get("released") and not p.get("reservationId")]
        return live[-1] if live else (None, None)

    def _release_others(self, hold_id):
        """At most one live hold per call: every other unbooked hold is
        released (nothing was written for it, so nothing is freed)."""
        out = []
        for k, p in self.state["proposals"].items():
            if k != hold_id and not p.get("reservationId") and not p.get("released"):
                p["released"] = True
                out.append(k)
        return out

    def _summary(self, hold_id):
        p = self.state["proposals"].get(hold_id) or {}
        return {"holdId": hold_id, "date": p.get("date"), "weekday": _weekday(p.get("date") or ""),
                "time": p.get("time"), "partySize": p.get("partySize")}

    @property
    def tracked(self) -> bool:
        """The executor is told what the caller heard (heard / not_heard): the
        read-back and the card terms are checked against the spoken replies.
        Every call and simulator turn is (agent.run_turn, session)."""
        return "heard" in self.state

    def heard(self, reply, lang=None, *, error=False):
        """The reply the caller heard at the end of this utterance (after any
        replacement by session). The one live hold counts as read back in
        this utterance only when the reply names its time and no other time
        or weekday (readback.covers); the card terms count as said only when
        the reply carries the fee rule. Called again in the same utterance
        (the reply was replaced), it first undoes its earlier verdict."""
        turn = self.turn
        before = self.state.get("heardBefore") or {}
        if self.state.get("heard") == turn and turn is not None:
            if before.get("readBack") is None:
                self.state.pop("readBack", None)
            else:
                self.state["readBack"] = before["readBack"]
            for p in self.state["proposals"].values():
                if p.get("guaranteeSaid") == turn:
                    p.pop("guaranteeSaid", None)
        self.state["heard"] = turn
        self.state["heardBefore"] = {"readBack": self.state.get("readBack")}
        hold_id, proposal = self._live()
        if error:
            self.state.pop("readBack", None)
            return
        if not proposal:
            return
        due = proposal.get("guaranteeDue")
        if due and not proposal.get("guaranteeSaid") and readback.mentions_terms(reply, fees=due == "fee"):
            proposal["guaranteeSaid"] = turn
        verdict = readback.covers(reply, proposal["date"], proposal["time"], lang or self.language,
                                  party=proposal.get("partySize"), today=self.today)
        if verdict is True:
            self.state["readBack"] = {"holdId": hold_id, "turn": turn}
        elif verdict is False:
            self.state.pop("readBack", None)

    def _replaced(self, hold_id):
        live_id, _ = self._live()
        return {"ok": False, "code": "holdReplaced", "hold": self._summary(hold_id),
                **({"live": self._summary(live_id)} if live_id else {}), "say": SAY["holdReplaced"]}

    def _read_back_problem(self, hold_id, proposal):
        """Why the caller's yes is not a yes to this hold (tracked executors)."""
        if not self.tracked or self.turn is None:
            return None
        rb = self.state.get("readBack") or {}
        turn = int(self.turn)
        fresh = rb.get("holdId") == hold_id and rb.get("turn") is not None and int(rb["turn"]) < turn and (
            proposal.get("turn") is None or int(rb["turn"]) >= int(proposal["turn"]))
        said = proposal.get("guaranteeSaid")
        if fresh and said is not None and int(rb["turn"]) < int(said):
            fresh = False        # the read-back came before the terms: read back again after them
        if not fresh:
            return {"ok": False, "code": "readBackMismatch", "hold": self._summary(hold_id),
                    "say": SAY["readBackMismatch"]}
        return None

    def _counter(self, result, time):
        if result.get("nearby"):
            self._reached("nearby_table")
        elif result.get("before") or result.get("after"):
            self._reached("other_time")
        before, after = result.get("before") or [], result.get("after") or []
        return {"held": False, "reason": result.get("reason") or "time_taken", "time": time,
                "suggest": agent_model.nearest_times(time, before, after) if (before or after) else [],
                "before": before[:3], "after": after[:3], "nearby": result.get("nearby") or [],
                "dayFull": bool(result.get("dayFull"))}

    # -- writes -----------------------------------------------------------------------

    def _booking_phone(self, spoken):
        """The number the booking is made under: the caller ID; a spoken number
        only when the network hid it (no booking under any number)."""
        if self.caller_known:
            return self.caller_phone
        if not spoken:
            return None
        return pa.normalize_e164(str(spoken))

    def create_reservation(self, args):
        hold_id = str(args["holdId"])
        proposal = self.state["proposals"].get(hold_id)
        if not proposal:
            return {"ok": False, "code": "unknown_hold"}
        if proposal.get("reservationId"):  # a retry of the same booking
            if proposal.get("pending"):
                return {"reservationId": proposal["reservationId"], "booked": False, "pending": True,
                        "guaranteeLinkSent": True, "sentence": copy.text(copy.GUARANTEE_LINK_SENT, self.language),
                        "say": SAY["guaranteeLinkSent"]}
            return {"reservationId": proposal["reservationId"], "booked": True}
        if proposal.get("linkFailed"):     # the card link could not be sent: the restaurant calls back
            return {"ok": False, "code": "guaranteeLinkFailed", "say": SAY["guaranteeLinkFailed"]}
        if args.get("confirmed") is not True:
            return {"ok": False, **_say("notConfirmed")}
        party = int(proposal["partySize"])
        if proposal.get("released"):      # at most one live hold: the caller's yes is never to this one
            return self._replaced(hold_id)
        link = self._guarantee_mode(party) == "link"
        noted = proposal.get("guaranteeSaid") is not None if self.tracked else proposal.get("guaranteeNoted")
        if link and not noted and self.tracked and proposal.get("guaranteeDue"):
            # Live test 2026-11-20: the terms were written before hold_table and never spoken.
            note = self._guarantee_note(party, new_booking=True)
            return {"ok": False, "code": "termsFirst", "holdId": hold_id, **note, "say": SAY["termsFirst"]}
        if link and not noted:
            # Held while the caller's bookings were found (no card terms then): the terms and the
            # card link are said in a new read-back before the pending booking is made.
            note = self._guarantee_note(party, new_booking=True)
            proposal.update({"guaranteeNoted": True, "turn": self.turn,
                             "guaranteeDue": "fee" if (note.get("guaranteeTerms") or {}).get("fees") else "card"})
            return {"ok": False, "code": "readBackFirst", "holdId": hold_id, **note,
                    "say": "Not booked yet. " + note["say"] + " After a clear yes, call create_reservation again."}
        if not self._heard_before(proposal.get("turn")):
            return {"ok": False, **_say("readBackFirst")}
        problem = self._read_back_problem(hold_id, proposal)
        if problem:
            return problem
        booked = next((p for p in self.state["proposals"].values()
                       if p.get("reservationId") and p["date"] == proposal["date"]), None)
        if booked:   # a second booking the same day in one call: a correction, not a new table
            return {"ok": False, "booking": {"reservationId": booked["reservationId"], "date": booked["date"],
                                             "time": booked["time"], "partySize": booked["partySize"]},
                    **_say("alreadyBooked")}
        problem = self._party_problem(int(proposal["partySize"]))
        if problem:      # the safety net behind get_availability and hold_table
            return {"ok": False, **problem}
        name = " ".join(str(args.get("name") or "").split())[:80]
        if not name:
            return {"ok": False, **_say("nameInvalid")}
        try:
            phone = self._booking_phone(args.get("phone"))
        except pa.PhoneAgentError:
            return {"ok": False, **_say("phoneInvalid")}
        link = self._guarantee_mode(int(proposal["partySize"])) == "link"
        if self.dry_run:
            reservation_id = f"sim-{args['holdId']}"
            if link:
                return self._held_for_card(proposal, reservation_id, None)
        else:
            body = {"date": proposal["date"], "startTime": proposal["time"], "tableIds": proposal["tableIds"],
                    "partySize": proposal["partySize"], "name": name, "source": "phone", "notifyGuest": True,
                    # Confirmations go out in Swedish or English (the booking languages).
                    "language": self._booking_language()}
            if phone:
                body["phone"] = phone
            if link:     # the caller ID (sms_reachable): the link goes to the number calling
                body["guarantee"] = "link"
            if args.get("note"):
                body["notes"] = " ".join(str(args["note"]).split())[:200]
            status, answer = self._call("create-pending-reservation", "POST",
                                        "/locations/{locationId}/reservations/manual", body=body, staff=True)
            if status == 409:  # taken since the proposal: re-offer from fresh availability
                self._cache.clear()
                result = self._offers(proposal["date"], proposal["time"], int(proposal["partySize"]))
                result.pop("bookable", None)
                result.pop("tableIds", None)
                return self._counter({**result, "reason": "time_taken"}, proposal["time"])
            if status == 400:
                message = str((answer or {}).get("error") or "")
                if message.startswith("name"):
                    return {"ok": False, **_say("nameInvalid")}
                if message.startswith("phone"):
                    return {"ok": False, **_say("phoneInvalid")}
                return {"ok": False, "code": "booking_failed"}
            if status not in (200, 201) or not answer.get("reservationId"):
                return {"ok": False, "code": "booking_failed"}
            reservation_id = answer["reservationId"]
            if isinstance(answer.get("guaranteeLink"), dict) and answer.get("status") == "pending":
                return self._held_for_card(proposal, reservation_id, answer)
        proposal["reservationId"] = reservation_id
        # The caller's own booking: it may be moved or cancelled in this call (also with a hidden number).
        self._remember_found([{"reservationId": reservation_id, "partySize": int(proposal["partySize"])}])
        self.state.update({"outcome": "booked", "reservationId": reservation_id,
                           "accepted": self._accepted(proposal)})
        return {"reservationId": reservation_id, "booked": True, "date": proposal["date"],
                "weekday": _weekday(proposal["date"]), "time": proposal["time"], "partySize": proposal["partySize"],
                "confirmationBy": "sms" if phone else None}

    def _booking_language(self):
        """sv for the Nordic languages, en for every other (the confirmation templates)."""
        lang = "sv" if self.language in ("sv", "no", "da", "fi") else "en"
        return lang if lang in pa.BOOKING_LANGUAGES else "en"

    def _sms(self, text):
        self.send_sms(tenant_id=self.tenant_id, location_id=self.location_id, to=self.caller_phone, text=text)

    def _held_for_card(self, proposal, reservation_id, answer):
        """The booking is pending until the card is added: text the caller
        the card link (guaranteeLink.url, a short plain link; their own caller
        ID only) and tell the model the
        fixed sentence. `answer` None: the simulator (nothing sent)."""
        if answer is not None:
            link = answer["guaranteeLink"]
            text = messages.guarantee_link_sms(answer, self.tenant_row, self.location, link["url"],
                                               int(link.get("minutes") or guarantee.PHONE_PENDING_MINUTES))
            try:
                self._sms(text)
            except Exception:  # noqa: BLE001 - no SMS: release the tables now, the restaurant calls back
                self.state["needsCallback"] = "guarantee"
                proposal["linkFailed"] = reservation_id
                self._release_unlinked(reservation_id)
                return {"ok": False, "code": "guaranteeLinkFailed", "say": SAY["guaranteeLinkFailed"]}
            self.state.setdefault("guaranteeLinks", {})[reservation_id] = {"text": text}
        proposal.update({"reservationId": reservation_id, "pending": True})
        self._remember_found([{"reservationId": reservation_id, "partySize": int(proposal["partySize"]),
                               "pending": True}])
        self.state.update({"outcome": "booked", "reservationId": reservation_id, "guaranteePending": True,
                           "accepted": self._accepted(proposal)})
        return {"reservationId": reservation_id, "booked": False, "pending": True, "guaranteeLinkSent": True,
                "date": proposal["date"], "weekday": _weekday(proposal["date"]), "time": proposal["time"],
                "partySize": proposal["partySize"], "heldMinutes": guarantee.PHONE_PENDING_MINUTES,
                "sentence": copy.text(copy.GUARANTEE_LINK_SENT, self.language), "say": SAY["guaranteeLinkSent"]}

    def _release_unlinked(self, reservation_id):
        """Cancel the pending booking whose link could not be texted (no
        guest message): nobody has its Stripe page, so it must not hold the
        tables until the expiry job. A failed cancel still expires by itself."""
        try:
            self._call("mark-arrived", "POST", "/locations/{locationId}/reservations/{reservationId}/status",
                       {"reservationId": reservation_id},
                       body={"status": "cancelled", "reason": "Kortlänken kunde inte skickas", "notifyGuest": False},
                       staff=True)
        except Exception:  # noqa: BLE001
            pass

    def _accepted(self, proposal):
        first = self.state.get("first") or {}
        if proposal.get("nearby"):
            return "nearby_table"
        if first.get("date") and proposal["date"] != first["date"]:
            return "next_day"
        if first.get("time") and proposal["time"] != first["time"]:
            return "other_time"
        return "none"

    def send_guarantee_link(self, args):
        """The card link of a booking create_reservation made in this call,
        texted again once (the caller says it did not arrive)."""
        sent = (self.state.get("guaranteeLinks") or {}).get(str(args.get("reservationId") or ""))
        if not sent:
            return {"ok": False, "code": "guarantee_not_available"}
        if sent.get("resent"):
            return {"ok": False, "code": "already_resent"}
        if not self.dry_run:
            try:
                self._sms(sent["text"])
            except Exception:  # noqa: BLE001
                return {"ok": False, "code": "guaranteeLinkFailed", "say": SAY["guaranteeLinkFailed"]}
        sent["resent"] = True
        return {"sent": True, "sentence": copy.text(copy.GUARANTEE_LINK_SENT, self.language)}

    def find_reservations(self, args):
        # Only the number the network gave us: a number the caller says could
        # be anyone's (audit #24). A hidden or malformed number finds nothing,
        # so the agent offers transfer_to_staff or request_callback.
        if not self.caller_known:
            return {"reservations": [], "callerHidden": True}
        phone = self.caller_phone
        start = max(date.fromisoformat(args["fromDate"]), self.today) if args.get("fromDate") else self.today
        if start > self.last_day:
            return {"reservations": []}
        # Only this caller's bookings, in one bounded range query.
        status, body = self._call("get-reservation", "GET", "/locations/{locationId}/reservations",
                                  query={"from": start.isoformat(), "to": self.last_day.isoformat(), "phone": phone},
                                  staff=True)
        if status == 200:
            answers = [(start.isoformat(), (status, body))]
        elif status == 400:
            answers = self._find_per_day(start)    # a get-reservation from before the range query (rollout)
        else:
            return {"ok": False, "code": "lookup_unavailable"}
        found = []
        for d, (status, body) in answers:
            if status != 200:
                continue
            for item in body.get("reservations") or body.get("items") or []:
                if item.get("customerPhone") == phone and item.get("status") in ("reserved", "pending", "held"):
                    when = item.get("date") or d
                    found.append({"reservationId": item["reservationId"], "date": when, "weekday": _weekday(when),
                                  "time": item.get("startTime"), "partySize": item.get("partySize"),
                                  **({"pending": True} if item.get("status") == "pending" else {})})
        found.sort(key=lambda f: (f["date"], f.get("time") or ""))
        self._remember_found(found)
        return {"reservations": found[:3]}

    def _find_per_day(self, start):
        days = []
        day = start
        while day <= self.last_day:
            days.append(day.isoformat())
            day += timedelta(days=1)

        def one(d):
            return d, self._call("get-reservation", "GET", "/locations/{locationId}/reservations",
                                 query={"date": d}, staff=True)

        with ThreadPoolExecutor(FIND_POOL) as pool:
            return list(pool.map(one, days))

    def _remember_found(self, found):
        """The caller's bookings this call may change, with the utterance each was first found in."""
        ids = self.state.setdefault("found", [])
        party = self.state.setdefault("foundParty", {})
        turns = self.state.setdefault("foundTurn", {})
        for f in found:
            rid = f["reservationId"]
            if rid not in ids:
                ids.append(rid)
            if isinstance(f.get("partySize"), int) and not isinstance(f["partySize"], bool):
                party[rid] = int(f["partySize"])
            if f.get("pending") and rid not in self.state.setdefault("foundPending", []):
                self.state["foundPending"].append(rid)   # waiting for the card: its party stays
            if self.turn is not None:
                turns.setdefault(rid, self.turn)

    def _own(self, reservation_id):
        if reservation_id not in (self.state.get("found") or []):
            raise pa.PhoneAgentError(404, "not_found")

    def _heard(self, reservation_id):
        """The booking was found in an earlier utterance than this one (so it can have been read back)."""
        return self._heard_before((self.state.get("foundTurn") or {}).get(reservation_id))

    def move_reservation(self, args):
        self._own(args["reservationId"])
        if args.get("confirmed") is not True:
            return {"ok": False, **_say("notConfirmed")}
        day, time = self._day(args["date"]), self._time(args["time"])
        party = self._party(args["partySize"]) if args.get("partySize") else None
        known = (self.state.get("foundParty") or {}).get(args["reservationId"])
        problem = self._date_problem(day)
        if not problem and party and party != known:
            # A new party size: a booking still waiting for its card keeps the
            # fees it was texted; any other may not grow into a card party.
            if args["reservationId"] in (self.state.get("foundPending") or []):
                problem = self._guarantee_callback(party)
            else:
                problem = self._party_problem(party, moving=True)
        elif not problem and party:
            problem = self._party_problem(party)
        if problem:
            return {"ok": False, **problem}
        if not self._heard(args["reservationId"]):
            return {"ok": False, **_say("readBackFirst")}
        if not self.dry_run:
            body = {"date": day, "startTime": time}
            if party:
                body["partySize"] = party
            size = party or (int(known) if known else None)
            check = self._offers(day, time, size) if size else None
            if check and check.get("bookable"):
                body["tableIds"] = check["tableIds"]
            # Not bookable in the view may still be the guest's own table at an
            # overlapping time: the route decides, keeping the booked tables.
            status, _ = self._call("mark-arrived", "PATCH", "/locations/{locationId}/reservations/{reservationId}",
                                   {"reservationId": args["reservationId"]}, body=body, staff=True)
            if status not in (200, 201):
                if not check or check.get("bookable"):
                    return {"ok": False, "code": "move_refused"}
                return {**self._counter(check, time), "ok": False, "code": "time_taken"}
        self.state.update({"outcome": "moved", "reservationId": args["reservationId"]})
        return {"ok": True, "moved": True, "date": day, "weekday": _weekday(day), "time": time}

    def cancel_reservation(self, args):
        self._own(args["reservationId"])
        if args.get("confirmed") is not True:
            return {"ok": False, **_say("notConfirmed")}
        if not self._heard(args["reservationId"]):
            return {"ok": False, **_say("readBackFirst")}
        if not self.dry_run:
            # The staff status route (no manage-link secret needed); "before it
            # starts" is the guest cancel rule, kept by find_reservations only
            # listing upcoming bookings.
            status, _ = self._call("mark-arrived", "POST",
                                   "/locations/{locationId}/reservations/{reservationId}/status",
                                   {"reservationId": args["reservationId"]},
                                   body={"status": "cancelled", "reason": "Avbokad via telefonassistenten"},
                                   staff=True)
            if status not in (200, 201):
                return {"ok": False, "code": "cancel_refused"}
        self.state.update({"outcome": "cancelled", "reservationId": args["reservationId"]})
        return {"ok": True, "cancelled": True}

    def get_menu(self, args):
        status, body = self._call("get-menu", "GET", "/locations/{locationId}/menu")
        if status != 200:
            return {"ok": False, "code": "menu_unavailable"}
        query = str(args.get("query") or "").lower()
        items = body.get("items") or body.get("menu") or []
        words = menu_words(query)
        hits = [i for i in items if any(w[:5] in json.dumps(i, ensure_ascii=False).lower() for w in words)] or items
        dishes = [{"name": i.get("name"), "price": i.get("price"), "currency": "SEK"}
                  for i in hits[:5] if i.get("name")]
        self.state.setdefault("outcome", "info")
        return {"dishes": dishes, "sourceLanguage": "sv"}

    def transfer_to_staff(self, args):
        to = self.loc.get("transferTo")
        if self.dry_run or not to or not self.voice:
            return {"answered": False}
        if getattr(self.voice, "deferred_tools", False):
            # The provider transfers (its transfer_to_number system tool in this turn's answer).
            self.state.setdefault("providerActions", []).append({"name": "transfer_to_number", "arguments": {
                "transfer_number": to, "reason": str(args.get("reason") or "caller_asked")[:100],
                "client_message": copy.text(copy.TRANSFERRING, self.language),
                "agent_message": "Samtal från telefonassistenten: " + str(args.get("reason") or "")[:100]}})
        result = self.voice.transfer(self.call_ref, to, pa.TRANSFER_TIMEOUT_S)
        self.state["transfer"] = {"answered": bool(result.answered)}
        if result.answered:
            self.state["outcome"] = "transferred"
        return {"answered": bool(result.answered)}

    @staticmethod
    def _topic(value):
        topic = " ".join(str(value or "").split())[:200]
        if not topic.startswith(CALLBACK_TOPICS):
            topic = ("other " + topic).strip()[:200]
        return topic

    def request_callback(self, args):
        phone = self.caller_phone or ""
        if args.get("phone"):
            try:
                phone = pa.normalize_e164(str(args["phone"]))
            except pa.PhoneAgentError:
                if not self.caller_known:
                    return {"ok": False, **_say("phoneInvalid")}
        topic = self._topic(args.get("topic"))
        if self.dry_run or not self.callbacks:
            callback_id = "sim-callback"
        else:
            callback_id = self.callbacks(name=" ".join(str(args.get("name") or "").split())[:80], phone=phone,
                                         topic=topic, preferred=args.get("preferredTime"))
        self.state.update({"outcome": "callback", "callbackId": callback_id})
        return {"callbackId": callback_id, "requested": True}

    def end_call(self, args):
        outcome = args.get("outcome")
        if outcome in pa.OUTCOMES and outcome not in ("refused", "error"):
            self.state.setdefault("outcome", outcome)
        self.state["summary"] = " ".join(str(args.get("summary") or "").split())[:200]
        # The channel hangs up after this reply (ElevenLabs: its end_call tool; Connect: Lex Close).
        self.state.setdefault("providerActions", []).append(
            {"name": "end_call", "arguments": {"reason": str(args.get("outcome") or "done")}})
        return {"ok": True}

    def counter_offer(self) -> str:
        """`counterOffer` for the call row and PhoneCall.Ended: the offer the
        caller accepted, or when nothing was booked the furthest step reached."""
        if self.state.get("outcome") == "booked":
            return self.state.get("accepted") or "none"
        return self.state.get("furthest") or "none"


# --- how to say what a result holds -------------------------------------------------------------
#
# The model turned "18:00" into "twelve in the evening" in Somali and "17:30"
# into "seven thirty" in Turkish: every time and date in a result gets its
# spoken English form (`readAs`), which the model only has to translate.

_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")
_ORDINALS = ("", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
             "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
             "eighteenth", "nineteenth", "twentieth", "twenty-first", "twenty-second", "twenty-third",
             "twenty-fourth", "twenty-fifth", "twenty-sixth", "twenty-seventh", "twenty-eighth", "twenty-ninth",
             "thirtieth", "thirty-first")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_EN_TEENS = ("ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
             "nineteen")
_EN_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_SV_ONES = ("noll", "ett", "två", "tre", "fyra", "fem", "sex", "sju", "åtta", "nio", "tio", "elva", "tolv",
            "tretton", "fjorton", "femton", "sexton", "sjutton", "arton", "nitton")
_SV_TENS = ("", "", "tjugo", "trettio", "fyrtio", "femtio", "sextio", "sjuttio", "åttio", "nittio")


# Common food words in the line's languages -> the Swedish stem the menu uses, so a dish asked for in
# another language still matches when the model passes the caller's own word (the tool asks for
# Swedish; this is the cheap fallback, no translation call). Matching is on the first five letters.
MENU_WORDS = {
    "kyckl": "chicken poulet pollo hähnchen huhn kurczak tavuk ayam piletina pile دجاج مرغ".split(),
    "fisk": "fish poisson pescado pesce fisch ryba balık ikan riba سمك ماهی".split(),
    "lax": "salmon saumon salmón salmone lachs łosoś somon salmon losos سلمون".split(),
    "räk": "shrimp shrimps prawn prawns crevette crevettes gamba gambas gamberi garnelen krewetki karides udang "
           "škampi روبيان میگو".split(),
    "nöt": "beef boeuf bœuf ternera carne manzo rind rindfleisch wołowina dana sapi govedina لحم گوشت".split(),
    "fläsk": "pork porc cerdo maiale schwein schweinefleisch wieprzowina babi svinjetina".split(),
    "lamm": "lamb agneau cordero agnello lammfleisch jagnięcina kuzu domba janjetina خروف بره".split(),
    "veget": "vegetarian végétarien vegetariano vegetarisch wegetariańskie wegetariański vejetaryen "
             "vegetarijansko نباتي گیاهی".split(),
    "vegan": "vegan végane vegano wegańskie vegansko".split(),
    "salla": "salad salade ensalada insalata salat sałatka salata سلطة سالاد".split(),
    "soppa": "soup soupe sopa zuppa suppe zupa çorba sup supa حساء شوربة سوپ".split(),
    "potat": "potato potatoes pomme patata patate kartoffel ziemniaki patates kentang krompir بطاطس سیب".split(),
    "ris": "rice riz arroz riso reis ryż pirinç nasi riža أرز برنج".split(),
    "ost": "cheese fromage queso formaggio käse ser peynir keju sir جبن پنیر".split(),
    "bröd": "bread pain pan pane brot chleb ekmek roti kruh خبز نان".split(),
    "glass": "ice cream glace helado gelato eis lody dondurma es sladoled بستنی".split(),
    "dess": "dessert desserts postre dolce nachtisch deser tatlı pencuci desert حلوى دسر".split(),
    "glute": "gluten glutenfree glutenfrei bezglutenowe glutensiz".split(),
    "lakto": "lactose lactosa lattosio laktose laktoza laktoz".split(),
    "barn": "kids children child enfant enfants niños bambini kinder dzieci çocuk anak djeca".split(),
    "svamp": "mushroom mushrooms champignon champignons setas funghi pilze grzyby mantar jamur gljive قارچ".split(),
}
_MENU_STEMS = {word: stem for stem, words in MENU_WORDS.items() for word in words}


def menu_words(query):
    """The search words of a menu question (more than three letters), each
    caller word with the Swedish stem of a known food word added."""
    out = []
    for word in str(query or "").lower().split():
        word = word.strip(".,?!:;\"'")
        if word in _MENU_STEMS:
            out.append(_MENU_STEMS[word])
        if len(word) > 3:
            out.append(word)
    return list(dict.fromkeys(out))


def number_words(n, lang="en"):
    """A whole number 0 to 999 999 in words: 1250 -> "one thousand two hundred
    fifty" / "tusen tvåhundrafemtio"."""
    n = int(n)
    if not 0 <= n < 1_000_000:
        return str(n)
    if lang == "sv":
        def below(k):          # 1..999, one word
            out = ""
            if k >= 100:
                out += ("" if k // 100 == 1 else _SV_ONES[k // 100]) + "hundra"
                k %= 100
            if k >= 20:
                out += _SV_TENS[k // 10] + (_SV_ONES[k % 10] if k % 10 else "")
            elif k:
                out += _SV_ONES[k]
            return out
        if n == 0:
            return "noll"
        thousands, rest = divmod(n, 1000)
        parts = []
        if thousands:
            parts.append(("" if thousands == 1 else below(thousands)) + "tusen")
        if rest:
            parts.append(below(rest))
        return " ".join(parts)

    def below_en(k):
        out = []
        if k >= 100:
            out.append(f"{_ONES[k // 100]} hundred")
            k %= 100
        if k >= 20:
            out.append(_EN_TENS[k // 10] + (f"-{_ONES[k % 10]}" if k % 10 else ""))
        elif k >= 10:
            out.append(_EN_TEENS[k - 10])
        elif k:
            out.append(_ONES[k])
        return " ".join(out)
    if n == 0:
        return "zero"
    thousands, rest = divmod(n, 1000)
    parts = [f"{below_en(thousands)} thousand"] if thousands else []
    if rest:
        parts.append(below_en(rest))
    return " ".join(parts)


def kronor_words(amount, lang="en"):
    """Whole kronor in words: 800 -> "eight hundred kronor" / "åttahundra kronor"."""
    amount = int(amount)
    if lang == "sv":
        return "en krona" if amount == 1 else f"{number_words(amount, 'sv')} kronor"
    return "one krona" if amount == 1 else f"{number_words(amount)} kronor"


def hours_words(hours, lang="en"):
    """24 -> "twenty-four hours" / "tjugofyra timmar"."""
    hours = int(hours)
    if lang == "sv":
        return "en timme" if hours == 1 else f"{number_words(hours, 'sv')} timmar"
    return "one hour" if hours == 1 else f"{number_words(hours)} hours"


def clock_words(value):
    """"19:30" -> "half past seven in the evening" (English, for the model)."""
    m = _CLOCK.match(str(value))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    part = ("at night" if hour < 5 else "in the morning" if hour < 12 else "in the afternoon" if hour < 17
            else "in the evening")
    if hour == 12 and minute == 0:
        return "twelve noon"
    if hour == 0 and minute == 0:
        return "midnight"
    h12 = hour % 12 or 12
    nxt = (hour + 1) % 12 or 12
    if minute == 0:
        said = f"{_ONES[h12]} o'clock"
    elif minute == 30:
        said = f"half past {_ONES[h12]}"
    elif minute == 15:
        said = f"quarter past {_ONES[h12]}"
    elif minute == 45:
        said = f"quarter to {_ONES[nxt]}"
    else:
        said = f"{minute} minutes past {_ONES[h12]}"
    return f"{said} {part}"


def date_words(value):
    """"2026-11-04" -> "Wednesday the fourth of November" (no year)."""
    if not _ISO_DAY.match(str(value)):
        return None
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return None
    return f"{_WEEKDAYS[day.weekday()]} the {_ORDINALS[day.day]} of {_MONTHS[day.month - 1]}"


def read_as(result):
    """The result with `readAs`: {"19:30": "half past seven in the evening",
    "2026-11-04": "Wednesday the fourth of November"} for every time and
    date it holds (none: the result unchanged)."""
    if not isinstance(result, dict):
        return result
    found = {}

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, str) and value not in found:
            said = clock_words(value) or date_words(value)
            if said:
                found[value] = said
    walk(result)
    return {**result, "readAs": found} if found else result
