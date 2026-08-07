"""Bounds every observation must clear, whoever sent it.

Discovery is trusted because we run it. Phones are not, because anyone
can POST. These rules apply to both — a bug in our own collector is as
capable of poisoning the list as a stranger is.
"""
from decimal import Decimal, InvalidOperation

MAX_PRICE = Decimal("100000")
MIN_PRICE = Decimal("0.01")


def _money(raw, field):
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        raise ValueError(f"{field} is not a number: {raw!r}")


def check(obs):
    for required in ("item_id", "store_id"):
        if not obs.get(required):
            raise ValueError(f"{required} is required")

    clearance = obs.get("clearance_price")
    listed = obs.get("list_price")

    if clearance is not None:
        c = _money(clearance, "clearance_price")
        if c < MIN_PRICE:
            raise ValueError(f"clearance_price below {MIN_PRICE}: {c}")
        if c > MAX_PRICE:
            raise ValueError(f"clearance_price above {MAX_PRICE}: {c}")
        if listed is not None and c > _money(listed, "list_price"):
            raise ValueError("clearance_price above list price")

    pct = obs.get("pct_off")
    if pct is not None:
        p = _money(pct, "pct_off")
        if not (0 <= p <= 100):
            raise ValueError(f"pct_off outside 0-100 percent: {p}")

    qty = obs.get("quantity")
    if qty is not None and int(qty) < 0:
        raise ValueError(f"negative quantity: {qty}")
