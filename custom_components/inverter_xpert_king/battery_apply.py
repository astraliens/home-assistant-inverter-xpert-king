"""Battery profile Apply: preflight, deterministic planner, transaction.

Everything here is a plain function operating on a client-like object
(`get_data`/`process_data`) - no asyncio, no Home Assistant imports. The
whole thing is meant to run inside one `Hub.async_run` executor job, so
it never releases the connection lock partway through a profile change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from itertools import permutations
from pathlib import Path
from typing import Callable

from .battery_chain import humanize_param_name, is_valid_ordered_value, neighbor_bounds
from .battery_current import CURRENT_SETTINGS, _format_current_param
from .const import PROFILE_FIELD_ORDER, PROFILE_FIELD_TO_QPIRI_PARAM
from .number import VOLTAGE_SETTINGS, _format_voltage

_VOLTAGE_SETTING_BY_PARAM = {s.param_name: s for s in VOLTAGE_SETTINGS}
_VOLTAGE_FIELD_BY_PARAM = {
    param: field_name
    for field_name, param in PROFILE_FIELD_TO_QPIRI_PARAM.items()
    if param in _VOLTAGE_SETTING_BY_PARAM
}
_CURRENT_SETTING_BY_PARAM = {s.param_name: s for s in CURRENT_SETTINGS}
_CURRENT_FIELD_BY_PARAM = {
    param: field_name
    for field_name, param in PROFILE_FIELD_TO_QPIRI_PARAM.items()
    if param in _CURRENT_SETTING_BY_PARAM
}


class FieldRef(str):
    """A translation placeholder value that is a raw profile-field
    identifier (one of the PROFILE_FIELD_* constants), not yet a label a
    user would recognize from the add-profile form.

    A plain `str` subclass on purpose: every consumer that expects a
    string placeholder (HA's exception formatting, this module's own
    tests) keeps working unchanged, while `isinstance(value, FieldRef)`
    gives select.py an actual type to check instead of guessing from the
    placeholder's key name.
    """

# The five voltage params in ascending chain order, exactly VOLTAGE_SETTINGS'
# own declared order - the planner's "natural order" tie-break relies on it.
_VOLTAGE_PARAM_ORDER = tuple(s.param_name for s in VOLTAGE_SETTINGS)

# QPIRI hands the battery type over already decoded to the label the
# `Battery type` entity shows ("User define", "Pylon", ...), never the PBT
# code, so the label is what gets compared and quoted back to the user.
# The expected label comes from the same command table process_data uses
# (PBT/QPIRI code "2"), not a second copy that could drift.
BATTERY_TYPE_PARAM = "battery_type"
_USER_DEFINE_BATTERY_TYPE_CODE = "2"
BATTERY_TYPE_REFUSAL_KEY = "battery_type_not_user_define"


def _battery_type_label_from_command_table(code: str) -> str:
    """Device-decoded battery-type label for `code` in inverter_commands.json."""
    definitions = json.loads(
        (Path(__file__).resolve().parent / "inverter" / "inverter_commands.json").read_text(
            encoding="utf-8"
        )
    )
    for field_conf in definitions.get("QPIRI", {}).values():
        for sub in field_conf.get("complex_params", {}).get("params", ()):
            if sub.get("name") == BATTERY_TYPE_PARAM:
                return sub["value"][code]
    raise KeyError(f"battery type code {code!r} missing from command table")


USER_DEFINE_BATTERY_TYPE = _battery_type_label_from_command_table(_USER_DEFINE_BATTERY_TYPE_CODE)

# Every other user-facing outcome on the Apply path, carried the same way
# (translation_key + placeholders) since T25. This module has no `hass`
# and must not localize anything itself; the caller (select.py) resolves
# a raw profile-field identifier through the add-profile form's own
# translated labels before raising, with an English fallback on a failed
# lookup - never a raw key or an exception.
#
# Which placeholder values are such an identifier is not left to a naming
# convention on the key - it is the value's own type. `FieldRef` marks a
# placeholder value as one of the PROFILE_FIELD_* constants (e.g.
# "back_to_discharge_voltage"), not yet the label a user would recognize.
# Forgetting to wrap a field id this way is not a silent mismatch: the
# resolver in select.py only localizes what carries this type, so an
# unwrapped raw id reaches the user's error text as-is - loud, not quiet.
PREFLIGHT_READ_FAILED_KEY = "apply_preflight_read_failed"
FIELD_NOT_REPORTED_KEY = "apply_preflight_field_not_reported"
VOLTAGE_RANGE_REFUSAL_KEY = "apply_preflight_voltage_out_of_range"
CURRENT_ALLOW_LIST_UNREADABLE_KEY = "apply_preflight_current_allow_list_unreadable"
CURRENT_NOT_OFFERED_KEY = "apply_preflight_current_not_offered"
VOLTAGE_ORDER_CONFLICT_KEY = "apply_preflight_voltage_order_conflict"
ROLLED_BACK_KEY = "apply_rolled_back"
ROLLBACK_INCOMPLETE_KEY = "apply_rollback_incomplete"
TRANSPORT_UNKNOWN_NOT_TRANSMITTED_KEY = "apply_transport_unknown_not_transmitted"
TRANSPORT_UNKNOWN_MAY_HAVE_REACHED_KEY = "apply_transport_unknown_may_have_reached"


class ApplyOutcome(Enum):
    """The five outcomes section D of the design requires - never collapse
    any of these into a generic "failed"."""

    SUCCESS = "success"
    PREFLIGHT_FAILED = "preflight_failed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_INCOMPLETE = "rollback_incomplete"
    TRANSPORT_UNKNOWN = "transport_unknown"


@dataclass
class StepOutcome:
    """One forward or rollback command attempted, for the result's
    journal - not the plan itself, which is internal to the planner.

    `transmitted` defaults to True (the ordinary case: the command did
    leave the socket) and is only ever set False when that is reliably
    known instead - the underlying sendall() itself raised, so nothing
    for this step reached the wire at all. It says nothing about whether
    the device received or acted on a transmitted command, only whether
    the attempt got that far.
    """

    field: str
    command: str
    target: object
    accepted: bool
    read_back: object
    confirmed: bool
    transmitted: bool = True


@dataclass
class ApplyMessage:
    """A failure text on its way out of this module.

    `translation_key`/`placeholders` carry the same information as `text`,
    structured for HA's `exceptions` mechanism instead of a finished
    sentence. A placeholder holding a `FieldRef` value is a raw
    profile-field identifier, not yet a word a user would recognize -
    this module has no `hass` to resolve it against the add-profile
    form's own translated labels, so the caller must.
    """

    text: str
    translation_key: str | None = None
    placeholders: dict[str, str] = field(default_factory=dict)


@dataclass
class ApplyResult:
    """Structured outcome of one Apply attempt.

    `final_snapshot`/`final_snapshot_parsed` are the most recent QPIRI
    read this transaction actually obtained (initial read for a no-op or
    a preflight failure that got that far; the last successful read
    otherwise) - callers merge it into the settings coordinator. Both are
    None only for TRANSPORT_UNKNOWN, where no read can be trusted as
    current.

    `translation_key`/`translation_placeholders` are set whenever this
    outcome has a wording of its own for the caller to show instead of
    `message` - which stays the plain English text either way, since it
    is what a caller logs and what a person searches transcripts for.
    """

    outcome: ApplyOutcome
    message: str
    sent_steps: list[StepOutcome] = field(default_factory=list)
    rollback_steps: list[StepOutcome] = field(default_factory=list)
    final_snapshot: dict[str, object] | None = None
    final_snapshot_parsed: list | None = None
    translation_key: str | None = None
    translation_placeholders: dict[str, str] = field(default_factory=dict)


@dataclass
class _PlannedStep:
    field: str
    param_name: str
    command: str
    target: object
    format: Callable[[object], str]

    def wire_param(self) -> str:
        return self.format(self.target)


def _read_snapshot(inverter_client) -> tuple[dict[str, object] | None, list | None]:
    raw = inverter_client.get_data('QPIRI')
    if not raw or raw == ['NAK']:
        return None, None
    parsed = inverter_client.process_data('QPIRI', raw)
    return {item['param']: item['value'] for item in parsed}, parsed


def _read_amp_list(inverter_client, command: str) -> list[int]:
    raw = inverter_client.get_data(command)
    if not raw or raw == ['NAK']:
        return []
    values = []
    for item in raw:
        if not item:
            continue
        try:
            values.append(int(item))
        except ValueError:
            continue
    return values


@dataclass
class VoltageOrderConflict:
    """Diagnostic for a target voltage chain no write order could reach.

    Captured at the first step that failed while checking the planner's
    preferred candidate order (decreases low-to-high, increases
    high-to-low) - a real, reproducible blocking point, even though other
    candidate orders may fail elsewhere. `lower_neighbor`/`upper_neighbor`
    are only set when that side actually narrowed the range below the
    field's own documented one.

    `lower_neighbor_is_unchanged`/`upper_neighbor_is_unchanged` are True
    only when that neighbour is not one of the fields this profile
    changes at all - so its value is definitely the inverter's own live
    reading, a value the user never typed, not this order's own choice of
    when to touch a field the profile does intend to move too. When
    False, the value is still a real value from a real simulated step,
    just not safe to describe as "left unchanged".
    """

    param_name: str
    target: float
    doc_range: tuple[float, float]
    lower_neighbor: str | None
    lower_neighbor_value: float | None
    lower_neighbor_is_unchanged: bool
    upper_neighbor: str | None
    upper_neighbor_value: float | None
    upper_neighbor_is_unchanged: bool


def plan_voltage_order(
    settings: tuple, initial: dict[str, float], target: dict[str, float]
) -> tuple[list[str] | None, VoltageOrderConflict | None]:
    """Deterministic ordering for the changed voltage fields.

    Returns `(order, None)` on success (`order` is `[]` when nothing
    changed) or `(None, conflict)` when no valid target-only order
    exists - `conflict` names the field, target and blocking neighbour so
    a caller can build an actionable message instead of a bare refusal.

    Pure function: `settings` supplies doc ranges, neighbours and the
    sentinel flag (the same `VOLTAGE_SETTINGS` the number platform uses);
    `initial`/`target` are param_name -> value for every field `settings`
    covers, already DNC-resolved. Each candidate order is simulated in
    full before being accepted - the returned order, once simulated
    forward, is guaranteed to only ever ask for one of the profile's own
    target values, never an intermediate one.
    """
    by_param = {s.param_name: s for s in settings}
    natural_order = [s.param_name for s in settings]
    changed = [p for p in natural_order if target[p] != initial[p]]
    if not changed:
        return [], None

    def simulate(order: list[str]) -> tuple[bool, VoltageOrderConflict | None]:
        state = dict(initial)
        for param in order:
            setting = by_param[param]
            lower_val = state.get(setting.lower_neighbor) if setting.lower_neighbor else None
            upper_val = state.get(setting.upper_neighbor) if setting.upper_neighbor else None
            value = target[param]
            if is_valid_ordered_value(
                setting.doc_range, value, lower_val, upper_val,
                allow_zero_sentinel=setting.allow_zero_sentinel,
            ):
                state[param] = value
                continue
            lo, hi = neighbor_bounds(setting.doc_range, lower_val, upper_val)
            blocked_by_lower = lower_val is not None and lower_val > 0 and lo > setting.doc_range[0]
            blocked_by_upper = upper_val is not None and upper_val > 0 and hi < setting.doc_range[1]
            return False, VoltageOrderConflict(
                param_name=param,
                target=value,
                doc_range=setting.doc_range,
                lower_neighbor=setting.lower_neighbor if blocked_by_lower else None,
                lower_neighbor_value=lower_val if blocked_by_lower else None,
                lower_neighbor_is_unchanged=blocked_by_lower and setting.lower_neighbor not in changed,
                upper_neighbor=setting.upper_neighbor if blocked_by_upper else None,
                upper_neighbor_value=upper_val if blocked_by_upper else None,
                upper_neighbor_is_unchanged=blocked_by_upper and setting.upper_neighbor not in changed,
            )
        return True, None

    # Preferred candidate: decreases low-to-high, increases high-to-low -
    # the common case needs no exhaustive search at all.
    decreases = [p for p in natural_order if p in changed and target[p] < initial[p]]
    increases = [p for p in reversed(natural_order) if p in changed and target[p] > initial[p]]
    preferred = decreases + increases
    ok, conflict = simulate(preferred)
    if ok:
        return preferred, None

    first_conflict = conflict
    for order in permutations(changed):
        ok, _ = simulate(list(order))
        if ok:
            return list(order), None
    return None, first_conflict


def _resolve_effective(profile: dict, initial: dict[str, object]) -> dict[str, object]:
    effective = {}
    for profile_field in PROFILE_FIELD_ORDER:
        param = PROFILE_FIELD_TO_QPIRI_PARAM[profile_field]
        override = profile.get(profile_field)
        effective[profile_field] = override if override is not None else initial.get(param)
    return effective


def _missing_qpiri_field(initial: dict[str, object]) -> str | None:
    for param in (*_VOLTAGE_FIELD_BY_PARAM, *_CURRENT_FIELD_BY_PARAM):
        if param not in initial or initial[param] is None:
            return param
    return None


def _voltage_step(profile_field: str, target_value: float) -> _PlannedStep:
    setting = _VOLTAGE_SETTING_BY_PARAM[PROFILE_FIELD_TO_QPIRI_PARAM[profile_field]]
    return _PlannedStep(
        field=profile_field,
        param_name=setting.param_name,
        command=setting.command,
        target=target_value,
        format=_format_voltage,
    )


def _current_step(profile_field: str, target_value: int) -> _PlannedStep:
    setting = _CURRENT_SETTING_BY_PARAM[PROFILE_FIELD_TO_QPIRI_PARAM[profile_field]]
    return _PlannedStep(
        field=profile_field,
        param_name=setting.param_name,
        command=setting.command,
        target=target_value,
        format=lambda amps, s=setting: _format_current_param(amps, s.amp_width),
    )


def _plan_currents(profile: dict, initial: dict[str, object], effective: dict[str, object]):
    """Explicit changed currents only, in a fixed field-declaration order.

    No relation between the two currents is encoded (amendment 4): they
    are independent fields here, ordered only by their position in
    `PROFILE_FIELD_ORDER` for determinism.
    """
    decreases, increases = [], []
    for profile_field in _CURRENT_FIELD_BY_PARAM.values():
        if profile.get(profile_field) is None:
            continue
        param = PROFILE_FIELD_TO_QPIRI_PARAM[profile_field]
        target_value = effective[profile_field]
        current_value = initial[param]
        if target_value == current_value:
            continue
        step = _current_step(profile_field, target_value)
        (decreases if target_value < current_value else increases).append(step)
    return decreases, increases


def _describe_voltage_conflict(conflict: VoltageOrderConflict) -> str:
    """Actionable text for a `VoltageOrderConflict`: the field and target
    the user asked for, and whichever live value stood in the way -
    explicitly the inverter's own current reading when that neighbour is
    not one of the fields this profile changes at all."""
    field = humanize_param_name(conflict.param_name)
    lo, hi = conflict.doc_range

    def _describe_neighbor(neighbor: str | None, value: float | None, is_unchanged: bool) -> str | None:
        if neighbor is None:
            return None
        name = humanize_param_name(neighbor)
        if is_unchanged:
            return f"{name} is currently {value} (this profile leaves it unchanged)"
        return f"{name} is currently {value}"

    blockers = [
        text for text in (
            _describe_neighbor(conflict.lower_neighbor, conflict.lower_neighbor_value, conflict.lower_neighbor_is_unchanged),
            _describe_neighbor(conflict.upper_neighbor, conflict.upper_neighbor_value, conflict.upper_neighbor_is_unchanged),
        )
        if text is not None
    ]
    if blockers:
        return (
            f"cannot reach {field} target {conflict.target} in any write order: "
            f"{' and '.join(blockers)}"
        )
    return f"{field} target {conflict.target} is outside its own {lo}-{hi} range"


def _voltage_order_conflict_translation(conflict: VoltageOrderConflict) -> tuple[str, dict[str, str]]:
    """translation_key/placeholders for a `VoltageOrderConflict`, mirroring
    `_describe_voltage_conflict`'s English text but keeping every field
    identifier as a raw profile-field name for the caller to localize.

    No blocker at all falls back to the same shape (and wording) as the
    plain doc-range refusal below - `_describe_voltage_conflict` produces
    the same sentence for both, so the two share one translation key.
    """
    field = FieldRef(_VOLTAGE_FIELD_BY_PARAM[conflict.param_name])
    if conflict.lower_neighbor is None and conflict.upper_neighbor is None:
        lo, hi = conflict.doc_range
        return VOLTAGE_RANGE_REFUSAL_KEY, {
            "field": field, "target": str(conflict.target), "min": str(lo), "max": str(hi),
        }
    placeholders = {"field": field, "target": str(conflict.target)}
    if conflict.lower_neighbor is not None:
        placeholders["lower_blocker_field"] = FieldRef(_VOLTAGE_FIELD_BY_PARAM[conflict.lower_neighbor])
        placeholders["lower_blocker_value"] = str(conflict.lower_neighbor_value)
        placeholders["lower_blocker_unchanged"] = "1" if conflict.lower_neighbor_is_unchanged else "0"
    if conflict.upper_neighbor is not None:
        placeholders["upper_blocker_field"] = FieldRef(_VOLTAGE_FIELD_BY_PARAM[conflict.upper_neighbor])
        placeholders["upper_blocker_value"] = str(conflict.upper_neighbor_value)
        placeholders["upper_blocker_unchanged"] = "1" if conflict.upper_neighbor_is_unchanged else "0"
    return VOLTAGE_ORDER_CONFLICT_KEY, placeholders


def _battery_type_refusal(initial: dict[str, object]) -> ApplyMessage | None:
    """Refusal for a plan that writes a voltage while the device reports a
    battery type other than `User define`, else None.

    A type the device did not report, or reported in a shape this build
    cannot decode, is not grounds for refusing - only a type that was
    actually read and is not `User define`.

    The device rule behind this is assumed, not measured (D045). If
    hardware evidence ever contradicts it, deleting this function and its
    single call site in `_preflight` is the whole rollback.
    """
    live = initial.get(BATTERY_TYPE_PARAM)
    if not isinstance(live, str) or not live or live == USER_DEFINE_BATTERY_TYPE:
        return None
    return ApplyMessage(
        text=(
            f"the inverter accepts battery voltage changes only while the battery type "
            f"is {USER_DEFINE_BATTERY_TYPE!r}, and it is currently {live!r} - change the "
            f"battery type yourself and try again; the integration never changes it"
        ),
        translation_key=BATTERY_TYPE_REFUSAL_KEY,
        placeholders={"current_type": live, "required_type": USER_DEFINE_BATTERY_TYPE},
    )


def _preflight(inverter_client, profile: dict):
    """Return (steps, initial_snapshot, initial_parsed, failure).

    `steps` is None and `failure` is an `ApplyMessage` on any preflight
    failure. Nothing is written to the device in this function.
    """
    initial, initial_parsed = _read_snapshot(inverter_client)
    if initial is None:
        return None, None, None, ApplyMessage(
            "could not read the inverter's current settings",
            translation_key=PREFLIGHT_READ_FAILED_KEY,
        )

    missing = _missing_qpiri_field(initial)
    if missing is not None:
        field = _VOLTAGE_FIELD_BY_PARAM.get(missing) or _CURRENT_FIELD_BY_PARAM.get(missing)
        return None, initial, initial_parsed, ApplyMessage(
            f"device did not report {missing}",
            translation_key=FIELD_NOT_REPORTED_KEY,
            placeholders={"field": FieldRef(field)},
        )

    effective = _resolve_effective(profile, initial)

    for profile_field, setting in (
        (f, _VOLTAGE_SETTING_BY_PARAM[PROFILE_FIELD_TO_QPIRI_PARAM[f]]) for f in _VOLTAGE_FIELD_BY_PARAM.values()
    ):
        if profile.get(profile_field) is None:
            continue
        value = effective[profile_field]
        lo, hi = setting.doc_range
        is_sentinel = setting.allow_zero_sentinel and value == 0.0
        if not (is_sentinel or lo <= value <= hi):
            return None, initial, initial_parsed, ApplyMessage(
                f"{profile_field} target {value} is outside its own {lo}-{hi} range",
                translation_key=VOLTAGE_RANGE_REFUSAL_KEY,
                placeholders={
                    "field": FieldRef(profile_field), "target": str(value), "min": str(lo), "max": str(hi),
                },
            )

    for profile_field in _CURRENT_FIELD_BY_PARAM.values():
        if profile.get(profile_field) is None:
            continue
        param = PROFILE_FIELD_TO_QPIRI_PARAM[profile_field]
        if effective[profile_field] == initial[param]:
            continue  # already a no-op - no set frame will be sent, no need to query the list either
        setting = _CURRENT_SETTING_BY_PARAM[param]
        allowed = _read_amp_list(inverter_client, setting.list_command)
        if not allowed:
            return None, initial, initial_parsed, ApplyMessage(
                f"could not read the device's current allow-list for {profile_field}",
                translation_key=CURRENT_ALLOW_LIST_UNREADABLE_KEY,
                placeholders={"field": FieldRef(profile_field)},
            )
        if effective[profile_field] not in allowed:
            return None, initial, initial_parsed, ApplyMessage(
                f"{profile_field} target {effective[profile_field]} A is not "
                f"currently offered by the device",
                translation_key=CURRENT_NOT_OFFERED_KEY,
                placeholders={"field": FieldRef(profile_field), "target": str(effective[profile_field])},
            )

    voltage_initial = {param: initial[param] for param in _VOLTAGE_FIELD_BY_PARAM}
    voltage_target = {
        param: effective[_VOLTAGE_FIELD_BY_PARAM[param]] for param in _VOLTAGE_FIELD_BY_PARAM
    }
    voltage_order, conflict = plan_voltage_order(VOLTAGE_SETTINGS, voltage_initial, voltage_target)
    if voltage_order is None:
        translation_key, placeholders = _voltage_order_conflict_translation(conflict)
        return None, initial, initial_parsed, ApplyMessage(
            _describe_voltage_conflict(conflict), translation_key, placeholders
        )

    # Currents are deliberately not gated by the battery type - nothing
    # suggests the device ties them to it, so a currents-only profile
    # still applies under any type.
    if voltage_order:
        refusal = _battery_type_refusal(initial)
        if refusal is not None:
            return None, initial, initial_parsed, refusal

    decreases, increases = _plan_currents(profile, initial, effective)
    voltage_steps = [
        _voltage_step(_VOLTAGE_FIELD_BY_PARAM[param], voltage_target[param]) for param in voltage_order
    ]
    steps = [*decreases, *voltage_steps, *increases]
    return steps, initial, initial_parsed, None


def _send_and_confirm(inverter_client, command: str, wire_param: str, param_name: str, target) -> tuple[
    StepOutcome, dict[str, object] | None, list | None
]:
    reply = inverter_client.get_data(command, wire_param)
    accepted = reply == ['ACK']
    # Captured immediately after the set call and before the confirming
    # QPIRI read that follows - that second call has its own sendall and
    # must not overwrite what this step's own send actually did.
    transmitted = getattr(inverter_client, "last_sendall_succeeded", True)
    snapshot, parsed = _read_snapshot(inverter_client)
    if snapshot is None:
        outcome = StepOutcome(
            field=param_name, command=command, target=target,
            accepted=accepted, read_back=None, confirmed=False, transmitted=transmitted,
        )
        return outcome, None, None
    read_back = snapshot.get(param_name)
    confirmed = accepted and read_back == target
    outcome = StepOutcome(
        field=param_name, command=command, target=target,
        accepted=accepted, read_back=read_back, confirmed=confirmed, transmitted=transmitted,
    )
    return outcome, snapshot, parsed


def _rollback(inverter_client, initial: dict[str, object], to_revert: list[_PlannedStep]) -> tuple[
    list[StepOutcome], dict[str, object] | None, list | None, bool
]:
    """Best-effort reverse rollback for `to_revert`, already in the order
    they must be undone in (most-recent-forward-step first).

    Each field is sent at most once. If a confirming read fails outright
    partway through, remaining fields are left unattempted rather than
    guessed at - the caller reports them as part of an incomplete
    rollback, not silently skipped.
    """
    results: list[StepOutcome] = []
    last_snapshot, last_parsed = None, None
    complete = True
    for step in to_revert:
        original = initial[step.param_name]
        outcome, snapshot, parsed = _send_and_confirm(
            inverter_client, step.command, step.format(original), step.param_name, original
        )
        results.append(outcome)
        if snapshot is None:
            complete = False
            break
        last_snapshot, last_parsed = snapshot, parsed
        if not outcome.confirmed:
            complete = False
    return results, last_snapshot, last_parsed, complete


def _transport_unknown_message(planned: _PlannedStep, outcome: StepOutcome, initial: dict[str, object]) -> str:
    """Honest wording for a step whose confirming read failed: never
    assert the setting *was* changed, only that a command may have
    reached the device and the result is unknown - naming the setting
    and the last value actually confirmed for it.

    `outcome.transmitted` tells the two cases apart. When it is reliably
    known that this step's own send never left the socket, that is
    stated plainly instead - the setting was not touched at all.
    """
    field = humanize_param_name(planned.param_name)
    last_confirmed = initial.get(planned.param_name)
    if not outcome.transmitted:
        return (
            f"the connection failed before the command changing {field} could be sent - "
            f"nothing was transmitted; {field} was last confirmed at {last_confirmed!r}"
        )
    return (
        f"a command changing {field} may have reached the inverter, but no confirming "
        f"reply came back afterward, so the resulting state is unknown; {field} was last "
        f"confirmed at {last_confirmed!r}"
    )


def apply_battery_profile(inverter_client, profile: dict) -> ApplyResult:
    """Run one full battery-profile Apply transaction.

    Meant to be the sole function body of a single `Hub.async_run` call -
    every read and write in here happens on one inverter_client, with
    nothing else able to interleave on the same connection.
    """
    steps, initial, initial_parsed, failure = _preflight(inverter_client, profile)
    if steps is None:
        return ApplyResult(
            outcome=ApplyOutcome.PREFLIGHT_FAILED,
            message=failure.text,
            final_snapshot=initial,
            final_snapshot_parsed=initial_parsed,
            translation_key=failure.translation_key,
            translation_placeholders=failure.placeholders,
        )

    if not steps:
        return ApplyResult(
            outcome=ApplyOutcome.SUCCESS,
            message="already matches the profile - nothing to change",
            final_snapshot=initial,
            final_snapshot_parsed=initial_parsed,
        )

    sent_steps: list[StepOutcome] = []
    confirmed_plan: list[_PlannedStep] = []
    last_snapshot, last_parsed = initial, initial_parsed

    for planned in steps:
        outcome, snapshot, parsed = _send_and_confirm(
            inverter_client, planned.command, planned.wire_param(), planned.param_name, planned.target
        )
        sent_steps.append(outcome)

        if snapshot is None:
            return ApplyResult(
                outcome=ApplyOutcome.TRANSPORT_UNKNOWN,
                message=_transport_unknown_message(planned, outcome, initial),
                sent_steps=sent_steps,
                final_snapshot=None,
                final_snapshot_parsed=None,
                translation_key=(
                    TRANSPORT_UNKNOWN_NOT_TRANSMITTED_KEY
                    if not outcome.transmitted
                    else TRANSPORT_UNKNOWN_MAY_HAVE_REACHED_KEY
                ),
                translation_placeholders={
                    "field": FieldRef(planned.field),
                    "last_confirmed": repr(initial.get(planned.param_name)),
                },
            )

        last_snapshot, last_parsed = snapshot, parsed

        if outcome.confirmed:
            confirmed_plan.append(planned)
            continue

        # Forward path stops here. Roll back every field, including this
        # one if it silently changed anyway, in reverse forward order.
        to_revert = list(confirmed_plan)
        if snapshot.get(planned.param_name) != initial.get(planned.param_name):
            to_revert.append(planned)
        to_revert.reverse()

        rollback_steps, rb_snapshot, rb_parsed, complete = _rollback(inverter_client, initial, to_revert)
        final_snapshot = rb_snapshot if rb_snapshot is not None else snapshot
        final_parsed = rb_parsed if rb_parsed is not None else parsed

        base_message = (
            f"inverter refused or did not confirm {planned.field} "
            f"(target {planned.target}, device now reports {outcome.read_back!r})"
        )
        rollback_placeholders = {
            "field": FieldRef(planned.field),
            "target": str(planned.target),
            "read_back": repr(outcome.read_back),
        }
        if complete:
            # Read-back is the only truth this transaction has: it cannot
            # tell "the device did not apply the reverting command" apart
            # from "the device's read-back lied afterward". "Restored"
            # would assert the former; only the latter half of that is
            # actually known, so state what was read, not what was done.
            return ApplyResult(
                outcome=ApplyOutcome.ROLLED_BACK,
                message=(
                    f"{base_message}; the device now reports every changed setting "
                    f"back at its previous value"
                ),
                sent_steps=sent_steps,
                rollback_steps=rollback_steps,
                final_snapshot=final_snapshot,
                final_snapshot_parsed=final_parsed,
                translation_key=ROLLED_BACK_KEY,
                translation_placeholders=rollback_placeholders,
            )
        return ApplyResult(
            outcome=ApplyOutcome.ROLLBACK_INCOMPLETE,
            message=(
                f"{base_message}; rollback could not be fully confirmed - "
                "battery is left in a mixed state, check settings manually"
            ),
            sent_steps=sent_steps,
            rollback_steps=rollback_steps,
            final_snapshot=final_snapshot,
            final_snapshot_parsed=final_parsed,
            translation_key=ROLLBACK_INCOMPLETE_KEY,
            translation_placeholders=rollback_placeholders,
        )

    return ApplyResult(
        outcome=ApplyOutcome.SUCCESS,
        message="profile applied",
        sent_steps=sent_steps,
        final_snapshot=last_snapshot,
        final_snapshot_parsed=last_parsed,
    )
