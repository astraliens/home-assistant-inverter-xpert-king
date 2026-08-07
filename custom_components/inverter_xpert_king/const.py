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
