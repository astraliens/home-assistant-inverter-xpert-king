"""Battery profile storage shape and helpers.

Split out of config_flow.py so the profile-select entity (select.py) can
read the same stored records without importing the config flow module -
config_flow.py itself imports select.py (for CURRENT_SETTINGS, to build
the profile form), so the reverse import would be circular.

entry.options[CONF_BATTERY_PROFILES] = {"version": N, "profiles": [...]}.
The version covers the whole list, not each record: a record on its own
can't say what an absent field means, only the schema it was written
under can. Every caller reads and writes this one shape and never touches
any other options key, so Advanced settings can never wipe profiles and
profile edits can never wipe the advanced flag.
"""
from __future__ import annotations

from typing import Any

from .const import BATTERY_PROFILE_SCHEMA_VERSION, CONF_BATTERY_PROFILES, PROFILE_FIELD_ORDER

PROFILE_ID_KEY = "id"
PROFILE_NAME_KEY = "name"
DO_NOT_CHANGE = "do_not_change"

# Distinguishes "key never written" from "key present with a null value" -
# `dict.get(key)` alone collapses both to None, which would let a stored
# `null` be treated as a fresh, writable container below.
_ABSENT = object()


def empty_profile_container() -> dict[str, Any]:
    return {"version": BATTERY_PROFILE_SCHEMA_VERSION, "profiles": []}


def profile_container(options) -> Any:
    """Return the stored profile container as-is, or a fresh empty one
    when the key has genuinely never been written.

    Never reshapes what was actually stored: a value that exists but
    isn't a container this build recognizes - wrong shape, a recognized
    shape at a version this build doesn't understand, or a stored `null`
    - is returned completely unchanged. `is_current_schema` is what turns
    that into a refusal; every caller must check it on this return value
    before ever building a new container from it, so nothing ever writes
    over a value it didn't positively recognize. Only a truly absent key
    becomes a fresh, current, empty container - that is what lets a
    brand new entry create its first profile.
    """
    raw = options.get(CONF_BATTERY_PROFILES, _ABSENT)
    if raw is _ABSENT:
        return empty_profile_container()
    return raw


def is_current_schema(container: Any) -> bool:
    """True only for a container this build both understands the shape
    of and recognizes the version of. False for anything else - not a
    dict, no profiles list, a non-integer version (a bool included -
    `True`/`False` are ints in Python but never a real version marker),
    or simply a version this build doesn't know."""
    if not isinstance(container, dict) or not isinstance(container.get("profiles"), list):
        return False
    version = container.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return version == BATTERY_PROFILE_SCHEMA_VERSION


def is_well_formed_record(record: Any) -> bool:
    """A record this build can safely show, edit, match or apply.

    Anything else - wrong shape, missing id/name, a field that isn't a
    real number or null - stays in storage but is never offered
    anywhere. `bool` is deliberately rejected even though it is a
    Python `int` subclass: `True`/`False` are never a valid voltage or
    current value for these fields.
    """
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get(PROFILE_ID_KEY), str) or not record[PROFILE_ID_KEY]:
        return False
    if not isinstance(record.get(PROFILE_NAME_KEY), str):
        return False
    for field_name in PROFILE_FIELD_ORDER:
        value = record.get(field_name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
    return True


def usable_profiles(container: dict[str, Any]) -> list[dict[str, Any]]:
    """Records safe to list, edit, delete or match against - [] if the
    whole list's schema version isn't one this build understands."""
    if not is_current_schema(container):
        return []
    return [record for record in container["profiles"] if is_well_formed_record(record)]


def profiles_sorted_by_name(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same records ordered by display name for listings - creation order
    in storage is untouched, this only affects what a menu shows. The key
    is case-insensitive so it does not depend on locale-specific casing
    rules; Latin and Cyrillic names simply fall into two separate runs."""
    return sorted(profiles, key=lambda record: record[PROFILE_NAME_KEY].casefold())


def replace_profile(container: dict[str, Any], record: dict[str, Any], editing_id: str | None) -> dict[str, Any]:
    """Drop the well-formed record being replaced (if any), keep every
    other record - malformed ones included - and append the new one."""
    kept = [
        p for p in container["profiles"]
        if not (is_well_formed_record(p) and p[PROFILE_ID_KEY] == editing_id)
    ]
    kept.append(record)
    return {"version": BATTERY_PROFILE_SCHEMA_VERSION, "profiles": kept}


def remove_profile(container: dict[str, Any], profile_id: str) -> dict[str, Any]:
    kept = [
        p for p in container["profiles"]
        if not (is_well_formed_record(p) and p[PROFILE_ID_KEY] == profile_id)
    ]
    return {"version": BATTERY_PROFILE_SCHEMA_VERSION, "profiles": kept}
