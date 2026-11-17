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

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from shared import agent_model, availability as shared_availability, booking_rules, guarantee
from shared.features import phone_agent as pa
from shared.features.phone_agent import copy
from shared.features.phone_agent import offers as of

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
                    "with `code` and `say` (pastDate, beyondHorizon, largeParty, guaranteeRequired): follow `say`.",
     "input_schema": {"type": "object", "required": ["date", "partySize"], "properties": {
         "date": _DATE, "partySize": _PARTY, "time": _TIME, "timeFrom": _TIME, "timeTo": _TIME}}},
    {"name": "hold_table",
     "description": "Check that a table can be booked at a time; writes nothing. Returns `holdId`, or other times "
                    "(`nearby` first if present, else exactly the times in `suggest`), or `code` and `say`.",
     "input_schema": {"type": "object", "required": ["date", "time", "partySize"], "properties": {
         "date": _DATE, "time": _TIME, "partySize": _PARTY,
         "tableIds": {"type": "array", "items": {"type": "string"},
                      "description": "only the tableIds of a `nearby` option the caller accepted"}}}},
    {"name": "create_reservation",
     "description": "Book a held table, only after the caller said a clear yes to your read-back of day, time, "
                    "number of guests and name.",
     "input_schema": {"type": "object", "required": ["holdId", "name", "confirmed"], "properties": {
         "holdId": {"type": "string"}, "name": {"type": "string", "description": "the name as the caller gave it"},
         "confirmed": _CONFIRMED,
         "phone": {"type": "string", "description": "only when the caller's number is hidden: the number they "
                                                   "gave and confirmed"},
         "note": {"type": "string", "description": "a short note for the restaurant, for example an allergy"}}}},
    {"name": "send_guarantee_link", "description": "Send the card guarantee link by SMS.",
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
                                        "not listed is not available today.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}},
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
    "notConfirmed": "Read back the day, time, number of guests and name, and wait for a clear yes first.",
    "nameInvalid": "The name could not be used. Ask for the name again.",
    "phoneInvalid": "The phone number could not be used. Ask for it again, or book without a number.",
}
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
                 voice=None, call_ref=None, callbacks=None, today=None, language="sv"):
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
        self.today = today or date.today()
        self.last_day = shared_availability.last_bookable_date(self.today, location)
        self.language = language or "sv"
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

    def _date_problem(self, day):
        when = date.fromisoformat(day)
        if when < self.today:
            return {"date": day, **_say("pastDate")}
        if when > self.last_day:
            return {"date": day, "lastDate": self.last_day.isoformat(),
                    **_say("beyondHorizon", days=shared_availability.BOOKING_HORIZON_DAYS,
                           lastDate=self.last_day.isoformat())}
        return None

    def _party_problem(self, party):
        if party > self.phone_max:
            self.state["needsCallback"] = "largeParty"
            return {"partySize": party, "largeParty": True, **_say("largeParty")}
        if guarantee.required(self.location, self.tenant_row, party):
            # The manual route never asks a card guarantee (ADR-L 13): a party the
            # online rules would guarantee gets a callback until the hotspot upgrade.
            self.state["needsCallback"] = "guarantee"
            return {"partySize": party, "guaranteeRequired": True, **_say("guaranteeRequired")}
        return None

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

    def _views(self, day, party):
        if self._staff_path(party):
            return None, self._staff_view(day), self._layout()
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
            return answer
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
        answer.update({"date": day, "weekday": _weekday(day), "partySize": party})
        if not answer["times"] and not answer.get("nearby"):
            answer["dayFull"] = True
            answer["nextDays"] = self._next_days(day, party, time)
            if answer["nextDays"]:
                self._reached("next_day")
        return answer

    def hold_table(self, args):
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
        hold_id = f"p{len(self.state['proposals']) + 1}"
        self.state["proposals"][hold_id] = {"date": day, "time": time, "partySize": party, "tableIds": table_ids,
                                            "nearby": bool(kind)}
        return {"holdId": hold_id, "date": day, "weekday": _weekday(day), "time": time, "partySize": party}

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
        proposal = self.state["proposals"].get(str(args["holdId"]))
        if not proposal:
            return {"ok": False, "code": "unknown_hold"}
        if proposal.get("reservationId"):  # a retry of the same booking
            return {"reservationId": proposal["reservationId"], "booked": True}
        if args.get("confirmed") is not True:
            return {"ok": False, **_say("notConfirmed")}
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
        if self.dry_run:
            reservation_id = f"sim-{args['holdId']}"
        else:
            body = {"date": proposal["date"], "startTime": proposal["time"], "tableIds": proposal["tableIds"],
                    "partySize": proposal["partySize"], "name": name, "source": "phone", "notifyGuest": True,
                    # Confirmations go out in Swedish or English (the booking languages).
                    "language": self._booking_language()}
            if phone:
                body["phone"] = phone
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
        proposal["reservationId"] = reservation_id
        first = self.state.get("first") or {}
        accepted = "none"
        if proposal.get("nearby"):
            accepted = "nearby_table"
        elif first.get("date") and proposal["date"] != first["date"]:
            accepted = "next_day"
        elif first.get("time") and proposal["time"] != first["time"]:
            accepted = "other_time"
        self.state.update({"outcome": "booked", "reservationId": reservation_id, "accepted": accepted})
        return {"reservationId": reservation_id, "booked": True, "date": proposal["date"],
                "weekday": _weekday(proposal["date"]), "time": proposal["time"], "partySize": proposal["partySize"],
                "confirmationBy": "sms" if phone else None}

    def _booking_language(self):
        """sv for the Nordic languages, en for every other (the confirmation templates)."""
        lang = "sv" if self.language in ("sv", "no", "da", "fi") else "en"
        return lang if lang in pa.BOOKING_LANGUAGES else "en"

    def send_guarantee_link(self, args):
        # Only after create_reservation answers guaranteeRequired, which the
        # manual route does not do in v1 (ADR-L 13, hotspot upgrade pending).
        return {"ok": False, "code": "guarantee_not_available"}

    def find_reservations(self, args):
        # Only the number the network gave us: a number the caller says could
        # be anyone's (audit #24). A hidden or malformed number finds nothing,
        # so the agent offers transfer_to_staff or request_callback.
        if not self.caller_known:
            return {"reservations": [], "callerHidden": True}
        phone = self.caller_phone
        start = max(date.fromisoformat(args["fromDate"]), self.today) if args.get("fromDate") else self.today
        days = []
        day = start
        while day <= self.last_day:
            days.append(day.isoformat())
            day += timedelta(days=1)

        def one(d):
            return d, self._call("get-reservation", "GET", "/locations/{locationId}/reservations",
                                 query={"date": d}, staff=True)

        with ThreadPoolExecutor(FIND_POOL) as pool:
            answers = list(pool.map(one, days))
        found = []
        for d, (status, body) in answers:
            if status != 200:
                continue
            for item in body.get("reservations") or body.get("items") or []:
                if item.get("customerPhone") == phone and item.get("status") in ("reserved", "pending", "held"):
                    when = item.get("date") or d
                    found.append({"reservationId": item["reservationId"], "date": when, "weekday": _weekday(when),
                                  "time": item.get("startTime"), "partySize": item.get("partySize")})
        self.state["found"] = [f["reservationId"] for f in found]
        self.state["foundParty"] = {f["reservationId"]: int(f["partySize"]) for f in found
                                    if isinstance(f.get("partySize"), int) and not isinstance(f["partySize"], bool)}
        return {"reservations": found[:3]}

    def _own(self, reservation_id):
        if reservation_id not in (self.state.get("found") or []):
            raise pa.PhoneAgentError(404, "not_found")

    def move_reservation(self, args):
        self._own(args["reservationId"])
        if args.get("confirmed") is not True:
            return {"ok": False, **_say("notConfirmed")}
        day, time = self._day(args["date"]), self._time(args["time"])
        party = self._party(args["partySize"]) if args.get("partySize") else None
        problem = self._date_problem(day) or (self._party_problem(party) if party else None)
        if problem:
            return {"ok": False, **problem}
        if not self.dry_run:
            body = {"date": day, "startTime": time}
            if party:
                body["partySize"] = party
            known = (self.state.get("foundParty") or {}).get(args["reservationId"])
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
        words = [w for w in query.split() if len(w) > 3]
        hits = [i for i in items if any(w[:5] in json.dumps(i, ensure_ascii=False).lower() for w in words)] or items
        dishes = [{"name": i.get("name"), "price": i.get("price"), "currency": "SEK"}
                  for i in hits[:5] if i.get("name")]
        self.state.setdefault("outcome", "info")
        return {"dishes": dishes}

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
