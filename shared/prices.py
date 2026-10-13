"""Highest price a restaurant can set on one product: a menu dish
(manage-menu) or a catering offer line (catering-offer), per item or
portion. A typo guard, like the order and offer totals - 10 000 kr covers
any real dish or bottle while catching an extra zero or three.

Checked when a price is SAVED only. Prices already stored are still read
and shown as they are, so an owner can open an over-limit dish and lower it.
The admin app shows the same limit (admin-front-end menuApi.ts and
catering/cateringApi.ts)."""

from decimal import Decimal

MAX_ITEM_PRICE_KR = 10_000
MAX_ITEM_PRICE_ORE = MAX_ITEM_PRICE_KR * 100
TOO_HIGH = f"price may be at most {MAX_ITEM_PRICE_KR} kr"


def check_kronor(value):
    """`value` (kr, already validated as a number >= 0) -> ValueError if above the limit."""
    if Decimal(value) > MAX_ITEM_PRICE_KR:
        raise ValueError(TOO_HIGH)
    return value
