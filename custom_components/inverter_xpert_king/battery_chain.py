"""Shared voltage-chain ordering rules.

Used both by the number platform's UI bounds and by the profile-apply
planner's step validity check, so the two can never drift apart: a value
the planner is about to write and a value the number entity would refuse
to show must always agree.
"""
from __future__ import annotations

# The frame's "nn.n" shape implies one decimal place, but no document
# fixes the step explicitly - this is an inference, not a vendor rule.
STEP = 0.1


def neighbor_bounds(
    doc_range: tuple[float, float],
    lower_neighbor_value: float | None,
    upper_neighbor_value: float | None,
) -> tuple[float, float]:
    """Return (min, max) for one threshold, tightened by its neighbours.

    A neighbour reporting the documented "until full" sentinel (0.0) does
    not constrain anything - it means that side of the chain is open, not
    that the boundary sits at zero. The returned range can be empty
    (lo > hi) when the neighbours themselves leave no room; callers decide
    what an empty range means for their own situation.
    """
    lo, hi = doc_range
    if lower_neighbor_value is not None and lower_neighbor_value > 0:
        candidate_lo = round(lower_neighbor_value + STEP, 1)
        if candidate_lo > lo:
            lo = candidate_lo
    if upper_neighbor_value is not None and upper_neighbor_value > 0:
        candidate_hi = round(upper_neighbor_value - STEP, 1)
        if candidate_hi < hi:
            hi = candidate_hi
    return (lo, hi)


def is_valid_ordered_value(
    doc_range: tuple[float, float],
    value: float,
    lower_neighbor_value: float | None,
    upper_neighbor_value: float | None,
    *,
    allow_zero_sentinel: bool = False,
) -> bool:
    """True when `value` may sit at this position in the chain right now.

    The sentinel value (exactly 0.0, only for settings that document one)
    is always valid on its own - it means "until full", not a numeric
    position - but a neighbour that is itself 0.0 already stops
    constraining anyone else via `neighbor_bounds` above, so nothing
    special is needed for that direction.
    """
    if allow_zero_sentinel and value == 0.0:
        return True
    lo, hi = neighbor_bounds(doc_range, lower_neighbor_value, upper_neighbor_value)
    if lo > hi:
        return False
    return lo <= value <= hi


def first_out_of_order_pair(
    ordered_values: list[tuple[str, float | None, bool]],
) -> tuple[str, float, str, float] | None:
    """Find the first pair of explicit values, anywhere in the chain and
    not just next to each other, whose relative order is impossible.

    `ordered_values` is (identifier, value, allow_zero_sentinel) triples
    already in ascending chain order; the identifier is opaque to this
    function - a caller's own field or param name. A value of None (left
    "do not change") takes part in no check here, since it resolves from
    the live device later and isn't known yet. The documented "until
    full" sentinel (0.0, only where a setting allows one) is not a
    numeric position in the chain either, so it is skipped the same way.

    Returns (lower_identifier, lower_value, upper_identifier, upper_value)
    for the first conflicting pair - both explicit, earlier one first -
    or None once every explicit pair clears the same one-step gap
    `neighbor_bounds` already enforces between two live neighbours.
    Checking only chain-adjacent explicit pairs would already be enough
    (that gap is transitive across any number of steps), but every pair
    is compared directly so the result never depends on that argument.
    """
    explicit = [
        (identifier, value)
        for identifier, value, allow_zero_sentinel in ordered_values
        if value is not None and not (allow_zero_sentinel and value == 0.0)
    ]
    for index, (lower_identifier, lower_value) in enumerate(explicit):
        for upper_identifier, upper_value in explicit[index + 1:]:
            if round(lower_value + STEP, 1) > upper_value:
                return lower_identifier, lower_value, upper_identifier, upper_value
    return None


def humanize_param_name(param_name: str) -> str:
    """Human-readable form of a QPIRI param name for user-facing text -
    never the raw snake_case id, a profile-field key, a translation key,
    or a Python repr."""
    return param_name.replace('_', ' ')
