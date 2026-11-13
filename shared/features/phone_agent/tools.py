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
from datetime import date, timedelta

from shared import agent_model, guarantee
from shared.features import phone_agent as pa
from shared.features.phone_agent import copy
from shared.features.phone_agent import offers as of

TOOLS = [
    {"name": "get_restaurant_info", "description": "Opening hours, special days, address and booking rules.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_availability", "description": "Free start times for a party on a day. With `time`, `suggest` "
                                                "holds the (at most three) closest free times: offer those.",
     "input_schema": {"type": "object", "required": ["date", "partySize"], "properties": {
         "date": {"type": "string"}, "partySize": {"type": "integer"}, "time": {"type": "string"},
         "timeFrom": {"type": "string"}, "timeTo": {"type": "string"}}}},
    {"name": "hold_table", "description": "Check that a table can be booked at a time; returns a proposal id or "
                                          "counter-offers: offer `nearby` first if present, else exactly the "
                                          "times in `suggest`. Writes nothing.",
     "input_schema": {"type": "object", "required": ["date", "time", "partySize"], "properties": {
         "date": {"type": "string"}, "time": {"type": "string"}, "partySize": {"type": "integer"},
         "tableIds": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "create_reservation", "description": "Book the proposal after the caller said yes.",
     "input_schema": {"type": "object", "required": ["holdId", "name", "partySize"], "properties": {
         "holdId": {"type": "string"}, "name": {"type": "string"}, "phone": {"type": "string"},
         "partySize": {"type": "integer"}, "note": {"type": "string"}, "language": {"type": "string"}}}},
    {"name": "send_guarantee_link", "description": "Send the card guarantee link by SMS.",
     "input_schema": {"type": "object", "required": ["reservationId"],
                      "properties": {"reservationId": {"type": "string"}}}},
    {"name": "find_reservations", "description": "The caller's upcoming bookings (by their number).",
     "input_schema": {"type": "object", "properties": {"phone": {"type": "string"}, "fromDate": {"type": "string"}}}},
    {"name": "move_reservation", "description": "Move a booking found by find_reservations.",
     "input_schema": {"type": "object", "required": ["reservationId", "date", "time"], "properties": {
         "reservationId": {"type": "string"}, "date": {"type": "string"}, "time": {"type": "string"},
         "partySize": {"type": "integer"}}}},
    {"name": "cancel_reservation", "description": "Cancel a booking found by find_reservations.",
     "input_schema": {"type": "object", "required": ["reservationId"], "properties": {
         "reservationId": {"type": "string"}, "reason": {"type": "string"}}}},
    {"name": "get_menu", "description": "Dishes on the menu matching a question.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "transfer_to_staff", "description": "Transfer the caller to the restaurant.",
     "input_schema": {"type": "object", "required": ["reason"], "properties": {"reason": {"type": "string"}}}},
    {"name": "request_callback", "description": "Ask the restaurant to call the caller back.",
     "input_schema": {"type": "object", "required": ["topic"], "properties": {
         "name": {"type": "string"}, "phone": {"type": "string"}, "topic": {"type": "string"},
         "preferredTime": {"type": "string"}}}},
    {"name": "end_call", "description": "End the call with an outcome and a one-line summary.",
     "input_schema": {"type": "object", "required": ["outcome", "summary"], "properties": {
         "outcome": {"type": "string"}, "summary": {"type": "string"}}}},
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)
FIND_DAYS = 14
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


def _special_day(day):
    """A special day as the site-config answers it ({date, kind, intervals?, note?}) in a few words."""
    if not isinstance(day, dict) or not day.get("date"):
        return ""
    kind = day.get("kind")
    if kind == "closed":
        what = "stängt"
    elif kind == "hours":
        what = "öppet " + ", ".join(f"{i.get('opensAt')}-{i.get('closesAt')}" for i in day.get("intervals") or [])
    else:
        what = "ingen bokning online" if kind == "no_online" else str(kind or "")
    note = f" ({' '.join(str(day['note']).split())[:80]})" if day.get("note") else ""
    return f"{day['date']} {what}{note}"


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
        self.language = language or "sv"
        self._cache = {}

    # -- plumbing ---------------------------------------------------------------------

    @property
    def claims(self):
        return {"tenant_id": self.tenant_id, "role": "staff_user", "sub": pa.AGENT_USER_PREFIX + self.location_id,
                "cognito:groups": ["staff_user"]}

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

    # -- reads ------------------------------------------------------------------------

    def _views(self, day, party):
        status, guest = self._call("get-availability", "GET", "/locations/{locationId}/availability",
                                   query={"date": day, "partySize": str(party)})
        if status != 200:
            raise pa.PhoneAgentError(502, "availability_unavailable")
        offers = self.loc.get("counterOffers") or {}
        staff, layout = {}, {}
        if offers.get("nearbyTables", True):
            s, staff = self._call("get-availability", "GET", "/locations/{locationId}/availability",
                                  query={"date": day})
            staff = staff if s == 200 else {}
            s, layout = self._call("list-layout-version", "GET", "/locations/{locationId}/layout/active")
            layout = layout if s == 200 else {}
        return guest, staff, layout

    @staticmethod
    def _day(value):
        return date.fromisoformat(value).isoformat()

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
        parts = []
        address = loc.get("address")
        if isinstance(address, dict):
            address = ", ".join(str(address[k]) for k in ("street", "postalCode", "city") if address.get(k))
        if address:
            parts.append(f"adress {address}")
        hours = loc.get("businessHours") or loc.get("openingHours")
        if hours:
            parts.append("öppettider " + json.dumps(hours, ensure_ascii=False)[:400])
        special = [_special_day(d) for d in (loc.get("specialDays") or [])[:6]]
        if special:
            parts.append("avvikande dagar " + "; ".join(x for x in special if x))
        rules = {k: loc.get(k) for k in ("maxPartySizeOnline", "bookingDurationHours", "gracePeriodMinutes")
                 if loc.get(k) is not None}
        if rules:
            parts.append("bokningsregler " + json.dumps(rules))
        return {"summary": "; ".join(parts) or (loc.get("name") or ""), "name": loc.get("name")}

    def get_availability(self, args):
        day, party = self._day(args["date"]), int(args["partySize"])
        time = args.get("time")
        first = self._note_request(day, time)
        if day != first["date"]:
            self._reached("next_day")
        guest, staff, layout = self._views(day, party)
        offers = self.loc.get("counterOffers") or {}
        answer = of.availability_answer(guest, staff, layout, party, time,
                                        nearby_tables=offers.get("nearbyTables", True),
                                        phone_only_tables=offers.get("phoneOnlyTables", True))
        frm, to = args.get("timeFrom"), args.get("timeTo")
        if frm or to:
            answer["times"] = [t for t in answer["times"] if (not frm or t >= frm) and (not to or t <= to)]
        if time and answer["times"]:
            answer["suggest"] = agent_model.nearest_times(time, answer["times"], ())
        return answer

    def hold_table(self, args):
        day, time, party = self._day(args["date"]), str(args["time"]), int(args["partySize"])
        first = self._note_request(day, time)
        if day != first["date"]:
            self._reached("next_day")
        guest, staff, layout = self._views(day, party)
        offers = self.loc.get("counterOffers") or {}
        wanted = [str(t) for t in args.get("tableIds") or []]
        result = of.offers(guest, staff, layout, time, party, nearby_tables=offers.get("nearbyTables", True),
                           phone_only_tables=offers.get("phoneOnlyTables", True))
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
        return {"holdId": hold_id, "date": day, "time": time, "partySize": party, "tableIds": table_ids}

    def _counter(self, result, time):
        if result.get("nearby"):
            self._reached("nearby_table")
        elif result.get("before") or result.get("after"):
            self._reached("other_time")
        before, after = result.get("before") or [], result.get("after") or []
        return {"held": False, "reason": result.get("reason") or "time_taken", "time": time,
                "suggest": agent_model.nearest_times(time, before, after) if (before or after) else [],
                "before": before, "after": after, "nearby": result.get("nearby") or [],
                "dayFull": bool(result.get("dayFull"))}

    # -- writes -----------------------------------------------------------------------

    def _guarantee_needed(self, party):
        # The manual route never asks a card guarantee (ADR-L 13): a party the
        # online rules would guarantee gets a callback until the hotspot upgrade.
        return guarantee.required(self.location, self.tenant_row, party)

    def create_reservation(self, args):
        proposal = self.state["proposals"].get(str(args["holdId"]))
        if not proposal:
            return {"ok": False, "code": "unknown_hold"}
        if proposal.get("reservationId"):  # a retry of the same booking
            return {"reservationId": proposal["reservationId"], "guaranteeRequired": False}
        if self._guarantee_needed(int(proposal["partySize"])):
            self.state["needsCallback"] = "guarantee"
            return {"ok": False, "code": "guarantee_callback"}
        name = " ".join(str(args.get("name") or "").split())[:80]
        phone = args.get("phone") or self.caller_phone
        if self.dry_run:
            reservation_id = f"sim-{args['holdId']}"
        else:
            body = {"date": proposal["date"], "startTime": proposal["time"], "tableIds": proposal["tableIds"],
                    "partySize": proposal["partySize"], "name": name, "source": "phone", "notifyGuest": True,
                    # Confirmations go out in Swedish or English (the booking languages).
                    "language": self._booking_language(args.get("language"))}
            if phone:
                body["phone"] = phone
            if args.get("note"):
                body["notes"] = str(args["note"])[:200]
            status, answer = self._call("create-pending-reservation", "POST",
                                        "/locations/{locationId}/reservations/manual", body=body, staff=True)
            if status in (400, 409):  # taken since the proposal: re-offer from fresh availability
                self._cache.clear()
                guest, staff, layout = self._views(proposal["date"], int(proposal["partySize"]))
                offers = self.loc.get("counterOffers") or {}
                result = of.offers(guest, staff, layout, proposal["time"], int(proposal["partySize"]),
                                   nearby_tables=offers.get("nearbyTables", True),
                                   phone_only_tables=offers.get("phoneOnlyTables", True))
                result.pop("bookable", None)
                result.pop("tableIds", None)
                return self._counter({**result, "reason": "time_taken"}, proposal["time"])
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
        return {"reservationId": reservation_id, "guaranteeRequired": False}

    def _booking_language(self, asked):
        lang = asked if asked in pa.BOOKING_LANGUAGES else self.language
        return lang if lang in pa.BOOKING_LANGUAGES else "en"

    def send_guarantee_link(self, args):
        # Only after create_reservation answers guaranteeRequired, which the
        # manual route does not do in v1 (ADR-L 13, hotspot upgrade pending).
        return {"ok": False, "code": "guarantee_not_available"}

    def find_reservations(self, args):
        phone = self.caller_phone or args.get("phone")
        if not phone:
            return {"reservations": []}
        start = date.fromisoformat(args["fromDate"]) if args.get("fromDate") else self.today
        found = []
        for offset in range(FIND_DAYS):
            day = (start + timedelta(days=offset)).isoformat()
            status, body = self._call("get-reservation", "GET", "/locations/{locationId}/reservations",
                                      query={"date": day}, staff=True)
            if status != 200:
                continue
            for item in body.get("reservations") or body.get("items") or []:
                if item.get("customerPhone") == phone and item.get("status") in ("reserved", "pending", "held"):
                    found.append({"reservationId": item["reservationId"], "date": item.get("date") or day,
                                  "time": item.get("startTime"), "partySize": item.get("partySize")})
        self.state["found"] = [f["reservationId"] for f in found]
        return {"reservations": found[:3]}

    def _own(self, reservation_id):
        if reservation_id not in (self.state.get("found") or []):
            raise pa.PhoneAgentError(404, "not_found")

    def move_reservation(self, args):
        self._own(args["reservationId"])
        if not self.dry_run:
            body = {"date": self._day(args["date"]), "startTime": str(args["time"])}
            if args.get("partySize"):
                body["partySize"] = int(args["partySize"])
            status, _ = self._call("mark-arrived", "PATCH", "/locations/{locationId}/reservations/{reservationId}",
                                   {"reservationId": args["reservationId"]}, body=body, staff=True)
            if status not in (200, 201):
                return {"ok": False, "code": "move_refused"}
        self.state.update({"outcome": "moved", "reservationId": args["reservationId"]})
        return {"ok": True}

    def cancel_reservation(self, args):
        self._own(args["reservationId"])
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
        return {"ok": True}

    def get_menu(self, args):
        status, body = self._call("get-menu", "GET", "/locations/{locationId}/menu")
        if status != 200:
            return {"ok": False, "code": "menu_unavailable"}
        query = str(args.get("query") or "").lower()
        items = body.get("items") or body.get("menu") or []
        words = [w for w in query.split() if len(w) > 3]
        hits = [i for i in items if any(w[:5] in json.dumps(i, ensure_ascii=False).lower() for w in words)] or items
        summary = ", ".join(f"{i.get('name')} {i.get('price', '')} kr".strip() for i in hits[:5] if i.get("name"))
        self.state.setdefault("outcome", "info")
        return {"summary": summary}

    def transfer_to_staff(self, args):
        to = self.loc.get("transferTo")
        if self.dry_run or not to or not self.voice:
            return {"answered": False}
        if getattr(self.voice, "deferred_tools", False):
            # The provider transfers (its transfer_to_number system tool in this turn's answer).
            self.state.setdefault("providerActions", []).append({"name": "transfer_to_number", "arguments": {
                "transfer_number": to, "reason": str(args.get("reason") or "caller_asked")[:100],
                "client_message": copy.pick(copy.TRANSFERRING, self.language),
                "agent_message": "Samtal från telefonassistenten: " + str(args.get("reason") or "")[:100]}})
        result = self.voice.transfer(self.call_ref, to, pa.TRANSFER_TIMEOUT_S)
        self.state["transfer"] = {"answered": bool(result.answered)}
        if result.answered:
            self.state["outcome"] = "transferred"
        return {"answered": bool(result.answered)}

    def request_callback(self, args):
        phone = args.get("phone") or self.caller_phone or ""
        topic = " ".join(str(args.get("topic") or "").split())[:200]
        if self.dry_run or not self.callbacks:
            callback_id = "sim-callback"
        else:
            callback_id = self.callbacks(name=str(args.get("name") or "")[:80], phone=phone, topic=topic,
                                         preferred=args.get("preferredTime"))
        self.state.update({"outcome": "callback", "callbackId": callback_id})
        return {"callbackId": callback_id}

    def end_call(self, args):
        outcome = args.get("outcome")
        if outcome in pa.OUTCOMES and outcome not in ("refused",):
            self.state.setdefault("outcome", outcome)
        self.state["summary"] = " ".join(str(args.get("summary") or "").split())[:200]
        if getattr(self.voice, "deferred_tools", False):
            self.state.setdefault("providerActions", []).append(
                {"name": "end_call", "arguments": {"reason": str(args.get("outcome") or "done")}})
        return {"ok": True}

    def counter_offer(self) -> str:
        """`counterOffer` for the call row and PhoneCall.Ended: the offer the
        caller accepted, or when nothing was booked the furthest step reached."""
        if self.state.get("outcome") == "booked":
            return self.state.get("accepted") or "none"
        return self.state.get("furthest") or "none"
