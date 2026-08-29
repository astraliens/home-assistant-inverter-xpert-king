"""Shared charge-current setting shape and wire formatting.

Split out of select.py so battery_apply.py's planner can build the same
MNCHGC/MUCHGC command frames the current select entities do, without
select.py (which imports battery_apply.py for ApplyOutcome) and
battery_apply.py importing each other - that would be a circular import.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CurrentSetting:
    """One QPIRI-backed charge-current control whose options come from the
    device's own allow-list query, never a hardcoded set of amps.

    `amp_width` is the digit count PROTOCOL.md documents for this command's
    amps field - 3 for MNCHGC (`MNCHGC0060`), 2 for MUCHGC (`MUCHGC030`).
    The two commands are not interchangeable padding-wise: an amps value
    that happens to already be N characters wide in the device's allow-list
    reply is not proof that N is the width this *set* command expects.
    """

    command: str
    list_command: str
    param_name: str
    amp_width: int


CURRENT_SETTINGS: tuple[CurrentSetting, ...] = (
    CurrentSetting(command="MNCHGC", list_command="QMCHGCR", param_name="max_charging_current", amp_width=3),
    CurrentSetting(command="MUCHGC", list_command="QMUCHGCR", param_name="max_ac_charging_current", amp_width=2),
)


def _format_current_param(amps: int, amp_width: int) -> str:
    """Single-block parallel index ("0") + amps at the command's documented
    width. `amp_width` is a floor, not a cap: P1 notes the AC field grows
    past its usual 2 digits once the value exceeds 99 A, and formatting
    with a minimum width (rather than truncating to it) produces that
    wider frame automatically instead of mangling a value that large.
    """
    return f"0{amps:0{amp_width}d}"
