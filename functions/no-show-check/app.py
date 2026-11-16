"""no-show-check (retired, safe no-op)

TRIGGER:
    EventBridge Scheduler (one-time) or a dlq-replay redelivery. Nothing in
    the application creates no-show-check-* schedules any more.

PURPOSE:
    None. No-shows are handled in mark-arrived (status no_show plus
    guarantee.run_charge). This handler only logs what it was given and
    returns, so a stray schedule or a replayed message never fails or
    lands in a dead-letter queue again. The function stays deployed only
    until the infrastructure removes it (Lambda, ECR repository, role,
    scheduler invoke role, stripe-webhook's create-no-show-check-schedule
    permission and the dlq-replay target); after that this directory can
    be deleted.

ENV_VARS:
    None read (the deployed ones are ignored).
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context=None):
    reservation_id = event.get("reservation_id") if isinstance(event, dict) else None
    logger.info(json.dumps({"noShowCheck": "retired", "reservationId": reservation_id
                            if isinstance(reservation_id, str) else None}))
    return {"ok": True, "result": "retired"}
