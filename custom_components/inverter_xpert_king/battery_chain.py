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


def chain_groups(settings) -> dict[str, object]:
    """Group id for every entry's `param_name`, derived only from that
    entry's own `lower_neighbor`/`upper_neighbor` links (`settings` has
    the shape of `number.VOLTAGE_SETTINGS`, duck-typed - this module
    does not import that one to avoid a cycle).

    Two settings connected through any chain of neighbour links land in
    the same group; a setting with neither link is its own singleton
    group. Nothing here hardcodes which params exist or how many groups
    there are (D047): the five voltage thresholds used to be one chain
    and are now two, purely because `PBDV`'s upper link and `PBFT`'s
    lower link were cleared in `VOLTAGE_SETTINGS` - this function's
    result follows that edit with no separate group list of its own to
    keep in step.
    """
    parent: dict[str, str] = {s.param_name: s.param_name for s in settings}

    def find(name: str) -> str:
        while parent[name] != name:
            name = parent[name]
        return name

    for s in settings:
        for neighbor in (s.lower_neighbor, s.upper_neighbor):
            if neighbor is None or neighbor not in parent:
                continue
            root_a, root_b = find(s.param_name), find(neighbor)
            if root_a != root_b:
                parent[root_a] = root_b

    return {name: find(name) for name in parent}


def first_out_of_order_pair(
    ordered_values: list[tuple[str, float | None, bool, object]],
) -> tuple[str, float, str, float] | None:
    """Find the first pair of explicit values in the same group, anywhere
    in that group and not just next to each other, whose relative order
    is impossible.

    `ordered_values` is (identifier, value, allow_zero_sentinel, group)
    quadruples already in ascending order within each group; identifier
    and group are both opaque to this function - a caller's own field or
    param name, and its own grouping key (`chain_groups` above is the
    usual source). Two entries with a different `group` are never
    compared against each other: D047 split the five voltage thresholds
    into two independent ordered groups with no relation between them,
    so a value in one group can never conflict with a value in another.
    A value of None (left "do not change") takes part in no check here,
    since it resolves from the live device later and isn't known yet.
    The documented "until full" sentinel (0.0, only where a setting
    allows one) is not a numeric position in the chain either, so it is
    skipped the same way.

    Returns (lower_identifier, lower_value, upper_identifier, upper_value)
    for the first conflicting pair - both explicit, earlier one first -
    or None once every explicit pair clears the same one-step gap
    `neighbor_bounds` already enforces between two live neighbours.
    Checking only chain-adjacent explicit pairs within a group would
    already be enough (that gap is transitive across any number of
    steps), but every same-group pair is compared directly so the result
    never depends on that argument.
    """
    explicit = [
        (identifier, value, group)
        for identifier, value, allow_zero_sentinel, group in ordered_values
        if value is not None and not (allow_zero_sentinel and value == 0.0)
    ]
    for index, (lower_identifier, lower_value, lower_group) in enumerate(explicit):
        for upper_identifier, upper_value, upper_group in explicit[index + 1:]:
            if lower_group != upper_group:
                continue
            if round(lower_value + STEP, 1) > upper_value:
                return lower_identifier, lower_value, upper_identifier, upper_value
    return None


def humanize_param_name(param_name: str) -> str:
    """Human-readable form of a QPIRI param name for user-facing text -
    never the raw snake_case id, a profile-field key, a translation key,
    or a Python repr."""
    return param_name.replace('_', ' ')
