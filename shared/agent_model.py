"""Agent model port (CONTRACTS.md 13, feature L, ADR-L decision 2): one
assistant turn of the phone agent loop, with tools. Not A's `TextModel`
(a single-shot completion). `client()` picks the simulator or
shared/agent_model_real.py (Bedrock Converse stream, owned by L) from
AGENT_MODEL_CLIENT; the model id is PHONE_AGENT_MODEL_ID.

Messages: `[{"role": "user" | "assistant", "content": str | [block]}]`,
blocks `{"type": "text", "text"}`, `{"type": "tool_use", "id", "name",
"input"}` and `{"type": "tool_result", "tool_use_id", "content"}`
(`content` a dict or a JSON string). Tools: `[{"name", ...}]` (or Bedrock's
`{"toolSpec": {"name", ...}}`).
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from shared import external_client


class AgentModelError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Turn:
    text: str | None = None
    tool_uses: tuple = ()
    stop_reason: str = "end_turn"            # "end_turn" | "tool_use" | "max_tokens"


class AgentModel:
    def converse(self, system, messages, tools, *, max_tokens) -> Turn:
        raise NotImplementedError


# --- the simulator's scripted intent table -----------------------------------------------

_SV, _EN = "sv", "en"
_I = re.IGNORECASE

# (intent, Swedish pattern, English pattern), first match wins
INTENTS = (
    ("human", r"\b(människa|riktig person|personal|prata med (någon|en person)|koppla mig)",
     r"\b(human|real person|speak to (someone|a person|staff)|talk to (someone|a person|staff))"),
    ("callback", r"\b(ring(a)? (mig )?tillbaka|ring(er)? upp mig|återkoppla)", r"\b(call (me )?back|callback)"),
    ("cancel", r"\b(avboka|avbeställa|ställa in (min|bokningen))", r"\bcancel"),
    ("move", r"\b(flytta|ändra (min|bokningen)|byta tid)", r"\b(move|reschedule|change (my )?(booking|reservation))"),
    ("availability", r"\b(lediga? (bord|tider)|finns det (plats|bord|lediga)|har ni (plats|bord))",
     r"\b(availab|any (free )?tables?|free tables?|have room)"),
    ("book", r"\b(boka|reservera|bord för)", r"\b(book|reserv|table for)"),
    ("menu", r"\b(meny|menyn|rätt(er)?|vegetari|vegan|allergi|gluten|laktos)",
     r"\b(menu|dish(es)?|vegetarian|vegan|allerg|gluten|lactose)"),
    ("hours", r"\b(öppet|öppettid|stänger|öppnar|adress|var ligger|julafton|helgdag)",
     r"\b(open|opening hours|close|closing|address|where are you|christmas|holiday)"),
    ("yes", r"^\W*(ja|japp|jajamen|absolut|gärna|det stämmer|okej då)\b", r"^\W*(yes|yeah|yep|sure|please do|correct)\b"),
    ("no", r"^\W*(nej|nä|nix)\b", r"^\W*(no|nope|no thanks)\b"),
)
ENTITY_INTENTS = ("number", "date", "time", "party_size")

_NUMBERS = {
    _SV: {"en": 1, "ett": 1, "två": 2, "tre": 3, "fyra": 4, "fem": 5, "sex": 6, "sju": 7, "åtta": 8, "nio": 9,
          "tio": 10, "elva": 11, "tolv": 12},
    _EN: {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12},
}
_WEEKDAYS = {
    _SV: ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"),
    _EN: ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
}
_WORDS = "|".join(sorted({*_NUMBERS[_SV], *_NUMBERS[_EN]}, key=len, reverse=True))
_PARTY = (re.compile(rf"\b(\d{{1,2}}|{_WORDS}) ?(personer|pers|gäster|people|persons|guests)\b", _I),
          re.compile(rf"\b(?:för|for) (\d{{1,2}}|{_WORDS})\b(?! ?(?:[:.]\d))", _I))
_TIME = (re.compile(r"\b(\d{1,2})[:.](\d{2})(?: ?(am|pm))?\b", _I),
         re.compile(r"\b(?:klockan|kl\.?|at) (\d{1,2})(?! ?(?:personer|people|guests|pers))(?: ?(am|pm))?\b", _I),
         re.compile(r"\b(\d{1,2}) ?(am|pm)\b", _I))
_PHONE = re.compile(r"(\+46|0)(?:[\s-]?\d){7,10}")
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_NAME = re.compile(r"\b(?:jag heter|mitt namn är|my name is|this is|i'm|i am) ([A-ZÅÄÖ][\wåäöéü-]+)", _I)
_SV_HINT = re.compile(r"[åäö]|\b(jag|och|vill|har|ni|det|hej|tack|ja|nej|okej|personer|klockan|imorgon|bord|"
                      r"för|en|tre|fyra|sex)\b", _I)

TOOL_FOR_INTENT = {"human": "transfer_to_staff", "callback": "request_callback", "cancel": "find_reservations",
                   "move": "find_reservations", "availability": "get_availability", "menu": "get_menu",
                   "hours": "get_restaurant_info"}

SENTENCES = {
    "ask_party_size": ("Hur många personer gäller det?", "How many people will it be?"),
    "ask_date": ("Vilken dag vill du komma?", "Which day would you like to come?"),
    "ask_name": ("Vilket namn ska bokningen stå på?", "What name should the booking be under?"),
    "ask_new_time": ("Vilken dag och tid vill du flytta till?", "Which day and time would you like instead?"),
    "unknown": ("Det kan jag tyvärr inte svara på. Vill du att någon ringer upp dig?",
                "I'm afraid I can't answer that. Would you like someone to call you back?"),
    "no": ("Okej. Kan jag hjälpa dig med något annat?", "Okay. Can I help you with anything else?"),
    "yes": ("Toppen. Vad kan jag hjälpa dig med?", "Great. What can I help you with?"),
    "cannot": ("Det kan jag tyvärr inte göra här. Vill du att någon ringer upp dig?",
               "I'm afraid I can't do that here. Would you like someone to call you back?"),
    "ask_time": ("Vilken tid passar?", "Which time suits you?"),
}
# ADR-L "Counter-offers": what the agent says when the requested time is
# taken. The nearby table (same time) is offered before other times; the
# closest free times on both sides of the requested time come next; a full
# day offers the next day once, then a callback. At most MAX_OFFERS times
# are spoken per turn.
MAX_OFFERS = 3
COUNTER_OFFERS = {
    "time_taken": ("Klockan {time} är tyvärr fullt. Jag kan erbjuda {times}. Vad passar?",
                   "I'm afraid {time} is fully booked. I can offer {times}. What suits you?"),
    "nearby_table": (("Klockan {time} finns inget vanligt bord ledigt, men jag kan ge er {table} klockan {time}. "
                      "Vill du ha det?"),
                     ("There is no regular table free at {time}, but I can give you {table} at {time}. "
                      "Would you like that?")),
    "near_time": ("Närmast klockan {time} har jag {times}. Vilken tid passar?",
                  "Closest to {time} I have {times}. Which time suits you?"),
    "next_day_full": ("Dagen efter är också fullbokad. Vill du att någon ringer upp dig om det blir ledigt?",
                      ("The next day is fully booked too. Would you like someone to call you back if something "
                       "opens up?")),
}
NEARBY_TABLE = {   # kind of nearby table (tool result) -> how it is said
    "adjacent_pair": ("två bord bredvid varandra", "two tables next to each other"),
    "larger": ("ett större bord", "a larger table"),
    "phone_only": ("ett bord vi bara bokar per telefon", "a table we only book by phone"),
}
CONFIRMATIONS = {   # tool -> (sv, en) said after its tool_result
    "get_restaurant_info": ("Här är det du frågade om: {summary}", "Here is what you asked about: {summary}"),
    "get_availability": ("Jag har {times}. Vilken tid passar?", "I have {times}. Which time suits you?"),
    "get_availability_full": ("Den dagen är tyvärr fullbokad. Vill du att jag kollar dagen efter?",
                              "That day is fully booked, I'm afraid. Shall I check the next day?"),
    "hold_table": ("Jag håller ett bord {date} klockan {time} för {partySize}. Ska jag boka det?",
                   "I'm holding a table on {date} at {time} for {partySize}. Shall I book it?"),
    "create_reservation": ("Klart, du är bokad. Du får en bekräftelse per sms.",
                           "Done, you're booked. You'll get a confirmation by text."),
    "send_guarantee_link": ("Jag har skickat en länk per sms där du lämnar kortgaranti. Jag frågar aldrig efter "
                            "kortuppgifter i telefon.",
                            "I've sent you a text with a link to leave a card guarantee. I never ask for card "
                            "details on the phone."),
    "find_reservations": ("Jag hittade din bokning {date} klockan {time}. Ska jag {action} den?",
                          "I found your booking on {date} at {time}. Shall I {action} it?"),
    "find_reservations_none": ("Jag hittar tyvärr ingen bokning.", "I'm afraid I can't find a booking."),
    "move_reservation": ("Klart, bokningen är flyttad.", "Done, your booking has been moved."),
    "cancel_reservation": ("Klart, bokningen är avbokad.", "Done, your booking is cancelled."),
    "get_menu": ("Här är det jag hittade på menyn: {summary}", "Here is what I found on the menu: {summary}"),
    "transfer_to_staff": ("Jag kopplar dig nu.", "I'm putting you through now."),
    "transfer_to_staff_unanswered": ("Ingen kunde svara just nu. Vill du att någon ringer upp dig?",
                                     "Nobody could answer right now. Would you like someone to call you back?"),
    "request_callback": ("Tack, vi ringer upp dig så snart vi kan.", "Thank you, we'll call you back as soon as we can."),
    "end_call": ("Tack för samtalet, hej då.", "Thank you for calling, goodbye."),
    "error": ("Något gick fel, förlåt. Vill du att någon ringer upp dig?",
              "Something went wrong, sorry. Would you like someone to call you back?"),
}
_ACTION = {"cancel": ("avboka", "cancel"), "move": ("flytta", "move")}


def _blocks(message):
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content or [])


def _text(message):
    return " ".join(b.get("text", "") for b in _blocks(message) if b.get("type") == "text").strip()


def _result(block):
    content = block.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return {"summary": content}
    return content if isinstance(content, dict) else {"items": content}


def _tool_names(tools):
    return {(t.get("toolSpec") or t).get("name") for t in tools or ()}


def _minute(clock):
    hour, minute = clock.split(":")
    return int(hour) * 60 + int(minute)


def nearest_times(requested, before, after, limit=MAX_OFFERS):
    """The ``limit`` free times closest to ``requested`` ("HH:MM") from the
    same-day lists on both sides of it, spoken in clock order; a tie in
    distance puts the earlier time first."""
    wanted = _minute(requested)
    ranked = sorted(set(before or ()) | set(after or ()), key=lambda t: (abs(_minute(t) - wanted), _minute(t)))
    return sorted(ranked[:limit], key=_minute)


def _joined(times, lang):
    times = list(times)
    if len(times) < 2:
        return times[0] if times else ""
    return ", ".join(times[:-1]) + (" eller " if lang == _SV else " or ") + times[-1]


def _is_full_day(result):
    if result.get("dayFull") is True or ("times" in result and not result["times"]):
        return True
    return result.get("held") is False and not (result.get("before") or result.get("after") or result.get("nearby"))


def _full_days(messages, today):
    """How many availability or hold answers said the whole day is full
    since the caller last named a date (the second one ends the day hopping
    and offers a callback). A new date from the caller starts the count
    again, so the next day is offered once per requested day."""
    count = 0
    for m in messages:
        if m.get("role") == "user" and _text(m) and "date" in entities(_text(m), today):
            count = 0
        count += sum(1 for b in _blocks(m) if b.get("type") == "tool_result" and _is_full_day(_result(b)))
    return count


def match_intent(text):
    """(intent, language) of one caller sentence, else (None, None)."""
    for intent, sv, en in INTENTS:
        if re.search(sv, text, _I):
            return intent, _SV
        if re.search(en, text, _I):
            return intent, _EN
    return None, None


def language_of(texts):
    """The language of the latest caller sentence that tells (its intent
    pattern, else Swedish words), else Swedish."""
    for text in reversed(texts):
        _, lang = match_intent(text)
        if lang:
            return lang
        if _SV_HINT.search(text):
            return _SV
        if re.search(r"[a-z]{2,}", text, _I):
            return _EN
    return _SV


def _number(word):
    word = word.lower()
    return int(word) if word.isdigit() else _NUMBERS[_SV].get(word) or _NUMBERS[_EN].get(word)


def entities(text, today):
    """The slots a caller sentence fills: date, time, partySize, phone, name."""
    found = {}
    low = text.lower()
    if m := _ISO_DATE.search(text):
        found["date"] = m.group(1)
    elif re.search(r"\b(idag|i dag|ikväll|i kväll|today|tonight)\b", low):
        found["date"] = today.isoformat()
    elif re.search(r"\b(imorgon|i morgon|tomorrow)\b", low):
        found["date"] = (today + timedelta(days=1)).isoformat()
    else:
        for lang in (_SV, _EN):
            for i, day in enumerate(_WEEKDAYS[lang]):
                if re.search(rf"\b{day}", low):
                    found["date"] = (today + timedelta(days=(i - today.weekday() - 1) % 7 + 1)).isoformat()
    for pattern in _TIME:
        if m := pattern.search(text):
            groups = m.groups()
            hour = int(groups[0])
            minute = int(groups[1]) if len(groups) > 1 and groups[1] and groups[1].isdigit() else 0
            suffix = (groups[-1] or "").lower() if groups[-1] and not groups[-1].isdigit() else ""
            if suffix == "pm" and hour < 12:
                hour += 12
            elif not suffix and 1 <= hour <= 11 and pattern is not _TIME[0]:
                hour += 12        # "klockan 7" on the phone means the evening
            if hour < 24 and minute < 60:
                found["time"] = f"{hour:02d}:{minute:02d}"
                break
    for pattern in _PARTY:
        if m := pattern.search(text):
            if n := _number(m.group(1)):
                found["partySize"] = n
                break
    if m := _PHONE.search(text):
        digits = re.sub(r"\D", "", m.group(0))
        found["phone"] = "+" + digits if m.group(1) == "+46" else "+46" + digits[1:]
    if m := _NAME.search(text):
        found["name"] = m.group(1)
    return found


class SimulatedAgentModel(external_client.Simulated, AgentModel):
    """Deterministic agent turns from the scripted intent table (INTENTS):
    the last caller message becomes the matching tool use (only tools that
    were offered) or a fixed Swedish/English sentence; a tool_result
    produces that tool's confirmation sentence (CONFIRMATIONS). Slots
    (date, time, party size, number, name) are gathered from every caller
    message since the last intent. `responses=[...]` (Turn, dict or str)
    overrides in order; `delay_ms` sleeps per call (latency harness);
    `today` anchors "imorgon"/weekdays."""

    error = AgentModelError

    def __init__(self, fail=None, responses=(), delay_ms=0, today=None):
        super().__init__(fail)
        self.responses = list(responses)
        self.delay_ms = delay_ms
        self.today = today or date.today()
        self._ids = 0

    def converse(self, system, messages, tools, *, max_tokens):
        last = messages[-1] if messages else {}
        self._call("converse", _text(last)[:40])
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000)
        if self.responses:
            return self._canned(self.responses.pop(0))
        if not messages:
            raise AgentModelError("validation")
        offered = _tool_names(tools)
        user_texts = [_text(m) for m in messages if m.get("role") == "user" and _text(m)]
        lang = language_of(user_texts)
        results = [b for b in _blocks(last) if b.get("type") == "tool_result"]
        if results:
            return self._after_tool(messages, results[-1], offered, lang)
        return self._after_caller(messages, offered, lang)

    # -- helpers ---------------------------------------------------------------------

    def _canned(self, item):
        if isinstance(item, Turn):
            return item
        if isinstance(item, str):
            return Turn(text=item)
        uses = tuple(u if isinstance(u, ToolUse) else ToolUse(u["id"], u["name"], dict(u.get("input") or {}))
                     for u in item.get("tool_uses") or ())
        return Turn(item.get("text"), uses, item.get("stop_reason") or ("tool_use" if uses else "end_turn"))

    def _say(self, key, lang, **fields):
        return self._offer(SENTENCES.get(key) or CONFIRMATIONS[key], lang, **fields)

    @staticmethod
    def _offer(pair, lang, **fields):
        sv, en = pair
        text = sv if lang == _SV else en
        return Turn(text=text.format(**fields) if fields else text)

    def _counter_offer(self, result, slots, lang):
        """The sentence after a hold that found the time taken (ADR-L
        counter-offers): nearby table first, else the closest times on both
        sides, else the day is full."""
        time = result.get("time") or slots.get("time", "")
        nearby = list(result.get("nearby") or [])
        if nearby:
            kind = nearby[0].get("kind", "adjacent_pair")
            table = NEARBY_TABLE.get(kind, NEARBY_TABLE["adjacent_pair"])[0 if lang == _SV else 1]
            return self._offer(COUNTER_OFFERS["nearby_table"], lang, time=time, table=table)
        times = nearest_times(time or "00:00", result.get("before"), result.get("after"))
        if times:
            return self._offer(COUNTER_OFFERS["time_taken"], lang, time=time, times=_joined(times, lang))
        return self._say("get_availability_full", lang)

    def _next_day_or_callback(self, messages, slots, offered, lang):
        """After a full day: the next day once, then a callback."""
        if _full_days(messages, self.today) >= 2 or "date" not in slots:
            return self._callback(slots, "bokning" if lang == _SV else "booking", offered, lang)
        next_day = (date.fromisoformat(slots["date"]) + timedelta(days=1)).isoformat()
        payload = {"date": next_day, "partySize": slots.get("partySize")}
        if "time" in slots:
            payload["time"] = slots["time"]
        return self._use("get_availability", payload, offered, lang)

    def _use(self, name, payload, offered, lang):
        if name not in offered:
            return self._say("cannot", lang)
        self._ids += 1
        return Turn(tool_uses=(ToolUse(f"sim-tool-{self._ids}", name, payload),), stop_reason="tool_use")

    def _context(self, messages):
        """The active intent (latest caller message with one), the slots
        gathered since it, and the latest tool use with its result."""
        intent, slots, unknown_text = None, {}, None
        for m in messages:
            if m.get("role") != "user" or not _text(m):
                continue
            text = _text(m)
            found, _ = match_intent(text)
            if found in ("yes", "no"):
                slots.update(entities(text, self.today))
                continue
            if found:
                intent, slots, unknown_text = found, {}, None
            elif not entities(text, self.today):
                intent, slots, unknown_text = None, {}, text
            slots.update(entities(text, self.today))
            if found == "callback" or (intent == "callback" and found is None):
                slots.setdefault("topic", text)
        last_use, last_result, offered_date, caller_date_after = None, None, None, False
        for m in messages:
            if m.get("role") == "user" and _text(m) and "date" in entities(_text(m), self.today):
                caller_date_after = True
            for b in _blocks(m):
                if b.get("type") == "tool_use":
                    last_use, last_result = b, None
                    if b.get("name") == "get_availability" and (b.get("input") or {}).get("date"):
                        offered_date, caller_date_after = b["input"]["date"], False
                elif b.get("type") == "tool_result" and last_use and b.get("tool_use_id") == last_use.get("id"):
                    last_result = _result(b)
        if offered_date and not caller_date_after:
            slots["date"] = offered_date      # the day the agent moved to (next day) is the day being booked
        return intent, slots, unknown_text, last_use, last_result

    def _booking_step(self, slots, offered, lang, *, hold):
        if "partySize" not in slots:
            return self._say("ask_party_size", lang)
        if "date" not in slots:
            return self._say("ask_date", lang)
        if hold and "time" in slots:
            return self._use("hold_table", {k: slots[k] for k in ("date", "time", "partySize")}, offered, lang)
        payload = {k: slots[k] for k in ("date", "partySize")}
        if "time" in slots:
            payload["time"] = slots["time"]
        return self._use("get_availability", payload, offered, lang)

    def _after_caller(self, messages, offered, lang):
        text = _text(messages[-1])
        said, _ = match_intent(text)
        intent, slots, unknown_text, last_use, last_result = self._context(messages)
        if said == "no":
            result = last_result or {}
            if (last_use or {}).get("name") == "hold_table" and result.get("held") is False and result.get("nearby"):
                times = nearest_times(slots.get("time", "00:00"), result.get("before"), result.get("after"))
                if times:     # the nearby table was declined: fall back to the closest times
                    return self._offer(COUNTER_OFFERS["time_taken"], lang, time=slots.get("time", ""),
                                       times=_joined(times, lang))
            return self._say("no", lang)
        if said == "yes":
            return self._confirm(messages, intent, slots, unknown_text, last_use, last_result, offered, lang)
        if intent is None:
            return self._say("unknown", lang)
        if (last_use or {}).get("name") == "hold_table" and (last_result or {}).get("holdId") and "name" in slots:
            return self._create(slots, last_result, offered, lang)
        if intent in ("book", "availability"):
            return self._booking_step(slots, offered, lang, hold=intent == "book")
        if intent == "move" and last_use and last_use.get("name") == "find_reservations" and \
                (last_result or {}).get("reservations") and "date" in slots and "time" in slots:
            found = last_result["reservations"][0]
            return self._use("move_reservation", {"reservationId": found["reservationId"], "date": slots["date"],
                                                  "time": slots["time"]}, offered, lang)
        if intent in ("cancel", "move"):
            payload = {"phone": slots["phone"]} if "phone" in slots else {}
            return self._use("find_reservations", payload, offered, lang)
        if intent == "human":
            return self._use("transfer_to_staff", {"reason": "caller_asked"}, offered, lang)
        if intent == "callback":
            return self._callback(slots, slots.get("topic") or text, offered, lang)
        if intent == "menu":
            return self._use("get_menu", {"query": text}, offered, lang)
        return self._use(TOOL_FOR_INTENT[intent], {}, offered, lang)

    def _callback(self, slots, topic, offered, lang):
        payload = {"topic": topic}
        payload.update({k: slots[k] for k in ("phone", "name") if k in slots})
        return self._use("request_callback", payload, offered, lang)

    def _create(self, slots, hold, offered, lang):
        if "name" not in slots:
            return self._say("ask_name", lang)
        payload = {"holdId": hold["holdId"], "name": slots["name"], "partySize": slots.get("partySize"),
                   "language": lang}
        payload.update({"phone": slots["phone"]} if "phone" in slots else {})
        return self._use("create_reservation", payload, offered, lang)

    def _confirm(self, messages, intent, slots, unknown_text, last_use, last_result, offered, lang):
        """A caller's "yes" continues what the agent last proposed."""
        name = (last_use or {}).get("name")
        result = last_result or {}
        if name == "hold_table" and result.get("holdId"):
            return self._create(slots, result, offered, lang)
        if name == "hold_table" and result.get("held") is False:
            nearby = list(result.get("nearby") or [])
            if nearby:        # "yes" to the nearby table: hold exactly those tables at the same time
                payload = {k: slots[k] for k in ("date", "time", "partySize") if k in slots}
                payload["tableIds"] = list(nearby[0].get("tableIds") or [])
                return self._use("hold_table", payload, offered, lang)
            if nearest_times(slots.get("time", "00:00"), result.get("before"), result.get("after")):
                return self._say("ask_time", lang)
            return self._next_day_or_callback(messages, slots, offered, lang)
        if name == "get_availability" and not result.get("times"):
            return self._next_day_or_callback(messages, slots, offered, lang)
        if name == "find_reservations" and result.get("reservations"):
            found = result["reservations"][0]
            if intent != "move":
                return self._use("cancel_reservation", {"reservationId": found["reservationId"]}, offered, lang)
            if "date" in slots and "time" in slots:
                return self._use("move_reservation", {"reservationId": found["reservationId"], "date": slots["date"],
                                                      "time": slots["time"]}, offered, lang)
            return self._say("ask_new_time", lang)
        if (name == "transfer_to_staff" and result.get("answered") is False) or unknown_text:
            return self._callback(slots, unknown_text or "callback", offered, lang)
        return self._say("yes", lang)

    def _after_tool(self, messages, block, offered, lang):
        intent, slots, _, last_use, _ = self._context(messages[:-1])
        name = (last_use or {}).get("name") if (last_use or {}).get("id") == block.get("tool_use_id") else None
        result = _result(block)
        if name is None or block.get("is_error") or result.get("ok") is False:
            return self._say("error", lang)
        if name == "get_availability":
            times = list(result.get("times") or [])[:MAX_OFFERS]
            if not times:
                if _full_days(messages, self.today) >= 2:
                    return self._offer(COUNTER_OFFERS["next_day_full"], lang)
                return self._say("get_availability_full", lang)
            requested = result.get("requestedTime") or (last_use or {}).get("input", {}).get("time")
            if requested:    # rank the whole day by distance to the asked time (the time itself included)
                near = nearest_times(requested, result.get("times"), ())
                return self._offer(COUNTER_OFFERS["near_time"], lang, time=requested, times=_joined(near, lang))
            return self._say("get_availability", lang, times=_joined(times, lang))
        if name == "hold_table" and result.get("held") is False:
            return self._counter_offer(result, slots, lang)
        if name == "create_reservation" and result.get("guaranteeRequired") and result.get("reservationId"):
            return self._use("send_guarantee_link", {"reservationId": result["reservationId"]}, offered, lang)
        if name == "transfer_to_staff" and result.get("answered") is False:
            return self._say("transfer_to_staff_unanswered", lang)
        if name == "find_reservations":
            found = list(result.get("reservations") or [])
            if not found:
                return self._say("find_reservations_none", lang)
            if intent == "move" and "date" in slots and "time" in slots:
                return self._use("move_reservation", {"reservationId": found[0]["reservationId"],
                                                      "date": slots["date"], "time": slots["time"]}, offered, lang)
            action = _ACTION.get(intent, _ACTION["cancel"])[0 if lang == _SV else 1]
            return self._say("find_reservations", lang, date=found[0].get("date", ""),
                             time=found[0].get("time", ""), action=action)
        if name == "hold_table":
            return self._say("hold_table", lang, date=result.get("date", slots.get("date", "")),
                             time=result.get("time", slots.get("time", "")),
                             partySize=result.get("partySize", slots.get("partySize", "")))
        if name in ("get_restaurant_info", "get_menu"):
            return self._say(name, lang, summary=result.get("summary") or "")
        return self._say(name, lang) if name in CONFIRMATIONS else self._say("yes", lang)


def client(**kwargs) -> AgentModel:
    if external_client.mode("agent_model") == external_client.REAL:
        from shared.agent_model_real import RealAgentModel
        return RealAgentModel()
    return SimulatedAgentModel(**kwargs)
