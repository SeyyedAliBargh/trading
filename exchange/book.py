"""Pure order-book helpers (no network)."""
from typing import Dict, List, Optional, Tuple

# Level structure is the one get_depth returns:
#   (price: float, qty: float, sum: float) sorted best-first
#   asks: ascending (best = lowest)
#   bids: descending (best = highest)


def fill_estimate(levels: List[Tuple[float, float, float]], notional_tmn: float) -> Dict:
    """Walk price levels (asks for BUY, bids for SELL) to cover `notional_tmn`.

    Returns dict(worst_price, vwap, qty, filled_notional, enough_depth).
    """
    filled_notional = 0.0
    qty = 0.0
    worst_price: Optional[float] = None
    for price, level_qty, _ in levels:
        remaining = notional_tmn - filled_notional
        if remaining <= 0:
            break
        take_notional = min(remaining, price * level_qty)
        take_qty = take_notional / price
        filled_notional += take_notional
        qty += take_qty
        worst_price = price
    vwap = filled_notional / qty if qty > 0 else None
    enough = filled_notional >= notional_tmn - 1e-9
    return {
        "worst_price": worst_price,
        "vwap": vwap,
        "qty": qty,
        "filled_notional": filled_notional,
        "enough_depth": enough,
    }


def fill_qty(levels: List[Tuple[float, float, float]], qty: float,
             max_price: Optional[float] = None) -> Dict:
    """Walk price levels to fill `qty` units, respecting an optional price cap.

    Returns dict(filled_qty, avg_price, worst_price, fills, enough_depth).
    fills is a list of (price, qty) tuples in execution order.
    """
    filled = 0.0
    cost = 0.0
    worst: Optional[float] = None
    fills: List[Tuple[float, float]] = []
    for price, level_qty, _ in levels:
        if max_price is not None and price > max_price + 1e-12:
            break
        remaining = qty - filled
        if remaining <= 1e-12:
            break
        take = min(level_qty, remaining)
        filled += take
        cost += take * price
        worst = price
        fills.append((price, take))
    avg = cost / filled if filled > 0 else None
    return {
        "filled_qty": filled,
        "avg_price": avg,
        "worst_price": worst,
        "fills": fills,
        "enough_depth": filled >= qty - 1e-9,
    }


def entry_limit_price(asks: List[Tuple[float, float, float]],
                      notional_tmn: float,
                      max_slippage_pct: float,
                      ticks_above: int,
                      price_precision: int) -> Optional[float]:
    """Compute the entry LIMIT BUY price that covers the full notional.

    price = worst_price + ticks_above * 10**-price_precision.
    Returns None if depth is insufficient or the price exceeds
    best_ask * (1 + max_slippage_pct).
    """
    if not asks:
        return None
    est = fill_estimate(asks, notional_tmn)
    if not est["enough_depth"] or est["worst_price"] is None:
        return None
    best_ask = asks[0][0]
    tick = 10 ** (-price_precision) if price_precision is not None else 0.0
    price = est["worst_price"] + ticks_above * tick
    if price > best_ask * (1 + max_slippage_pct):
        return None
    return price
