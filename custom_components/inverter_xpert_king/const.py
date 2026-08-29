# This is the internal name of the integration, it should also match the directory
# name for the integration.
DOMAIN = "inverter_xpert_king"

# Reject inverter responses whose CRC doesn't match. Flip to False to fall
# back to the old unchecked behaviour; frame parsing is identical either way.
VERIFY_RESPONSE_CRC = True

# Config entry data keys for the device identity (serial number, or a
# generated fallback when the serial can't be read). Stored so it survives
# restarts and is never re-derived from the connection address.
CONF_DEVICE_KEY = "device_key"
CONF_DEVICE_KEY_SOURCE = "device_key_source"

# The pre-migration identity (derived from the gateway address), frozen
# into entry.data the first time it's needed so a later reconfigure of
# the host can never change it again. See _get_or_freeze_legacy_device_id
# in __init__.py.
CONF_LEGACY_DEVICE_KEY = "legacy_device_key"

# Consecutive successful parses of a command that must come back without a
# given field before the matching entity is disabled. A single missed poll
# (timeout, bad CRC, ...) proves nothing about whether the field is
# actually supported.
#
# The same count applies to a different signal: consecutive NAKs for a
# command as a whole, treated as evidence the command itself is
# unsupported rather than just a bad poll. See FieldAbsenceTracker in
# sensor.py for both.
ORPHAN_ABSENCE_THRESHOLD = 3

# How far the clock QT reads back may drift from the time we asked DAT to
# set before a sync counts as failed. Covers the round trip between the
# two requests, not clock quality - QT is read immediately after DAT
# replies, so a device with a working RTC should land well inside this.
TIME_SYNC_TOLERANCE_SECONDS = 5

# Integration option gating the output/battery setting controls that can
# hurt the battery or drop the load if misclicked. Defaults to off; the
# select/number platforms create nothing under this gate until it is
# turned on, and existing registry entries are disabled rather than
# removed while it stays off.
CONF_ADVANCED_SETTINGS = "advanced_settings"
DEFAULT_ADVANCED_SETTINGS = False

# Named battery-setting overrides, stored in entry.options as
# {"version": BATTERY_PROFILE_SCHEMA_VERSION, "profiles": [record, ...]}.
# The version travels with the list, not with each record, because a
# record on its own can't say what an absent field means - only the
# schema it was written under can. A container whose version this build
# doesn't recognize is left on disk untouched and treated as having no
# usable profiles, rather than guessed at or silently dropped.
CONF_BATTERY_PROFILES = "battery_profiles"
BATTERY_PROFILE_SCHEMA_VERSION = 1

# Recommended cap, not a protocol limit - just keeps names reasonable in
# a select's option list.
PROFILE_NAME_MAX_LENGTH = 64

# Storage keys for the seven overridable settings, in the order the Add/Edit
# form presents them. Distinct from number.py/select.py's own QPIRI
# param_name spellings (e.g. "battery_recharge_voltage") because a profile
# field is a user-facing override, not a decoded wire value.
PROFILE_FIELD_UNDER_VOLTAGE = "under_voltage"
PROFILE_FIELD_BACK_TO_GRID_VOLTAGE = "back_to_grid_voltage"
PROFILE_FIELD_BULK_VOLTAGE = "bulk_voltage"
PROFILE_FIELD_FLOAT_VOLTAGE = "float_voltage"
PROFILE_FIELD_BACK_TO_DISCHARGE_VOLTAGE = "back_to_discharge_voltage"
PROFILE_FIELD_MAX_AC_CHARGING_CURRENT = "max_ac_charging_current"
PROFILE_FIELD_MAX_CHARGING_CURRENT = "max_charging_current"

PROFILE_FIELD_ORDER = (
    PROFILE_FIELD_UNDER_VOLTAGE,
    PROFILE_FIELD_BACK_TO_GRID_VOLTAGE,
    PROFILE_FIELD_BULK_VOLTAGE,
    PROFILE_FIELD_FLOAT_VOLTAGE,
    PROFILE_FIELD_BACK_TO_DISCHARGE_VOLTAGE,
    PROFILE_FIELD_MAX_AC_CHARGING_CURRENT,
    PROFILE_FIELD_MAX_CHARGING_CURRENT,
)

# Profile field name -> QPIRI decoded param name. The two currents already
# share a spelling (a profile field is the QPIRI param name verbatim); the
# five voltages don't, since the profile field describes the setting's
# role in the chain while the QPIRI param name is number.py's own wire
# decoding key. Kept in one place so config_flow.py's form/storage and
# battery_apply.py's planner can never carry two different mappings.
PROFILE_FIELD_TO_QPIRI_PARAM = {
    PROFILE_FIELD_UNDER_VOLTAGE: "battery_under_voltage",
    PROFILE_FIELD_BACK_TO_GRID_VOLTAGE: "battery_recharge_voltage",
    PROFILE_FIELD_BULK_VOLTAGE: "battery_bulk_voltage",
    PROFILE_FIELD_FLOAT_VOLTAGE: "battery_float_voltage",
    PROFILE_FIELD_BACK_TO_DISCHARGE_VOLTAGE: "battery_redischarge_voltage",
    PROFILE_FIELD_MAX_AC_CHARGING_CURRENT: "max_ac_charging_current",
    PROFILE_FIELD_MAX_CHARGING_CURRENT: "max_charging_current",
}

# Repair issues for an Apply that leaves the battery's actual state unclear
# (amendments 5 and 5a) - never raised for a clean preflight refusal (nothing
# was written) or a confirmed rollback (the previous settings are back), both
# of which leave a known state. The two keys share one issue id per inverter
# (see select._apply_issue_id) but carry different wording, because the two
# situations are not the same thing: an unconfirmed rollback attempt is not
# the same as a connection lost with no rollback attempted at all.
ISSUE_PROFILE_APPLY_MIXED_STATE = "battery_profile_apply_mixed_state"
ISSUE_PROFILE_APPLY_TRANSPORT_UNKNOWN = "battery_profile_apply_transport_unknown"
