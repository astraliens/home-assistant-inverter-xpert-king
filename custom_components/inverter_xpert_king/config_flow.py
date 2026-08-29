from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .battery_chain import chain_groups, first_out_of_order_pair
from .battery_profile_storage import (
    DO_NOT_CHANGE,
    PROFILE_ID_KEY as _PROFILE_ID,
    PROFILE_NAME_KEY as _PROFILE_NAME,
    is_current_schema as _is_current_schema,
    is_well_formed_record as _is_well_formed_record,  # noqa: F401 - test coverage calls this directly
    profile_container as _profile_container,
    remove_profile as _remove_profile,
    replace_profile as _replace_profile,
    usable_profiles as _usable_profiles,
)
from .const import (
    CONF_ADVANCED_SETTINGS,
    CONF_BATTERY_PROFILES,
    CONF_DEVICE_KEY,
    CONF_DEVICE_KEY_SOURCE,
    DEFAULT_ADVANCED_SETTINGS,
    DOMAIN,
    PROFILE_FIELD_ORDER,
    PROFILE_FIELD_TO_QPIRI_PARAM,
    PROFILE_NAME_MAX_LENGTH,
)
from .hub import Hub, generate_device_key, normalize_serial
from .number import VOLTAGE_SETTINGS
from .select import CURRENT_SETTINGS

_LOGGER = logging.getLogger(__name__)

# Host/port only — shared by reconfigure so flipping the address never
# offers (or silently resets) the advanced-settings option.
CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
    }
)

# Initial setup also surfaces the advanced-settings gate so first-time
# users see that the riskier controls exist. Same key and default as the
# options flow; the chosen value is stored in entry.options.
USER_DATA_SCHEMA = CONNECTION_SCHEMA.extend(
    {
        vol.Required(
            CONF_ADVANCED_SETTINGS, default=DEFAULT_ADVANCED_SETTINGS
        ): bool,
    }
)


def normalize_host(raw: str) -> str:
    """Return stripped host, or raise ValueError if it cannot possibly work."""
    host = raw.strip()
    # Reject only values that cannot work; reachability is decided by the connect attempt.
    if not host or " " in host:
        raise ValueError("invalid_host")
    return host


async def validate_input(
    hass: HomeAssistant, data: dict, *, resolve_device_key: bool = False
) -> dict[str, Any]:
    """Validate host/port and confirm the gateway answers.

    `resolve_device_key` is only requested for a fresh add - reconfiguring
    an existing entry changes the address, never the identity.
    """
    try:
        host = normalize_host(data[CONF_HOST])
    except ValueError as err:
        raise InvalidHost from err

    port = data[CONF_PORT]

    hub = Hub(hass, host, port)
    try:
        if not await hub.test_connection():
            raise CannotConnect

        result: dict[str, Any] = {"title": host}
        if resolve_device_key:
            serial = await hub.async_run(hub.inverter_client.get_serial)
            device_key = normalize_serial(serial)
            if device_key:
                result["device_key"] = device_key
                result["device_key_source"] = "qid"
            else:
                result["device_key"] = generate_device_key()
                result["device_key_source"] = "generated"
    finally:
        await hub.async_close()

    return result


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Inverter Xpert King."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input, resolve_device_key=True)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidHost:
                errors[CONF_HOST] = "invalid_host"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Outside the try block: this can itself raise AbortFlow, which
                # must propagate to the flow manager rather than be reported as
                # a plain validation error.
                await self.async_set_unique_id(info["device_key"])
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: info["title"], CONF_PORT: user_input[CONF_PORT]}
                )
                # Persist stripped host so the entry matches what we validated.
                # The checkbox also goes into data: that field is guaranteed
                # at create time on every supported HA. Setup copies it into
                # options (the single source of truth for the gate) before
                # platforms run, so the choice is honoured on first load even
                # if the options= kwarg on create_entry is ignored.
                advanced = user_input.get(
                    CONF_ADVANCED_SETTINGS, DEFAULT_ADVANCED_SETTINGS
                )
                entry_data = {
                    CONF_HOST: info["title"],
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_DEVICE_KEY: info["device_key"],
                    CONF_DEVICE_KEY_SOURCE: info["device_key_source"],
                    CONF_ADVANCED_SETTINGS: advanced,
                }
                return self.async_create_entry(
                    title=info["title"],
                    data=entry_data,
                    options={CONF_ADVANCED_SETTINGS: advanced},
                )

        schema = USER_DATA_SCHEMA
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(USER_DATA_SCHEMA, user_input)
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_reconfigure(self, user_input=None):
        """Update the gateway address of an existing entry.

        Only host/port change here - identity (device_key) is set once at
        setup or migration and is not re-derived from a reconfigure.

        Looks the entry up by the entry_id HA puts in the flow context
        instead of the newer `_get_reconfigure_entry()` helper, so this
        flow only needs the reconfigure step itself (present since the
        feature was introduced), not that later convenience wrapper.
        """
        errors: dict[str, str] = {}
        reconfigure_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data={
                        **reconfigure_entry.data,
                        CONF_HOST: info["title"],
                        CONF_PORT: user_input[CONF_PORT],
                    },
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidHost:
                errors[CONF_HOST] = "invalid_host"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                CONNECTION_SCHEMA, reconfigure_entry.data
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "OptionsFlowHandler":
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


# --- Battery profile form -----------------------------------------------
#
# Storage shape and CRUD helpers now live in battery_profile_storage.py -
# select.py needs the same reads and this module already imports select.py
# for CURRENT_SETTINGS, so the storage helpers can't live here without a
# circular import the other way.

_VOLTAGE_SETTING_BY_PARAM = {s.param_name: s for s in VOLTAGE_SETTINGS}
_VOLTAGE_SETTING_BY_FIELD = {
    field_name: _VOLTAGE_SETTING_BY_PARAM[param]
    for field_name, param in PROFILE_FIELD_TO_QPIRI_PARAM.items()
    if param in _VOLTAGE_SETTING_BY_PARAM
}
_VOLTAGE_FIELD_BY_PARAM = {
    param: field_name
    for field_name, param in PROFILE_FIELD_TO_QPIRI_PARAM.items()
    if param in _VOLTAGE_SETTING_BY_PARAM
}
# Profile field names in ascending physical chain order (VOLTAGE_SETTINGS'
# own order) - not PROFILE_FIELD_ORDER, which is only the form's
# presentation order and puts bulk_voltage ahead of float_voltage.
_VOLTAGE_CHAIN_FIELD_ORDER = tuple(_VOLTAGE_FIELD_BY_PARAM[s.param_name] for s in VOLTAGE_SETTINGS)

# Which of the two independent ordered groups (D047) each field belongs
# to, derived from VOLTAGE_SETTINGS' own neighbour links - not a second
# copy of the grouping, just this form's field names attached to it.
_VOLTAGE_CHAIN_GROUP_BY_PARAM = chain_groups(VOLTAGE_SETTINGS)

_CURRENT_SETTING_BY_PARAM = {s.param_name: s for s in CURRENT_SETTINGS}
_CURRENT_SETTING_BY_FIELD = {
    field_name: _CURRENT_SETTING_BY_PARAM[param]
    for field_name, param in PROFILE_FIELD_TO_QPIRI_PARAM.items()
    if param in _CURRENT_SETTING_BY_PARAM
}


def _get_hub(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> Hub | None:
    domain_data = hass.data.get(DOMAIN)
    if not domain_data:
        return None
    return domain_data.get(entry.entry_id)


async def _live_current_options(hass: HomeAssistant, entry: config_entries.ConfigEntry, field: str) -> list[str]:
    """Display strings for one current field's live allow-list, or [] if
    the device can't be asked right now. Never a hardcoded fallback."""
    hub = _get_hub(hass, entry)
    if hub is None or not hub.online:
        return []
    setting = _CURRENT_SETTING_BY_FIELD[field]
    try:
        raw_options = await hub.async_read_amp_list(setting.list_command)
    except Exception:  # noqa: BLE001 - any transport failure just means "no list"
        _LOGGER.debug("Could not read %s for the profile form", setting.list_command)
        return []
    seen: set[str] = set()
    display: list[str] = []
    for raw in raw_options:
        try:
            text = str(int(raw))
        except ValueError:
            continue
        if text not in seen:
            seen.add(text)
            display.append(text)
    return display


def _voltage_selector(setting) -> selector.NumberSelector:
    lo, hi = setting.doc_range
    if setting.allow_zero_sentinel:
        lo = 0.0
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=lo,
            max=hi,
            step=0.1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="V",
        )
    )


def _current_selector(options: list[str], *, stale_value: str | None = None) -> selector.SelectSelector:
    """Options for one current field: Do not change, plus the device's
    live allow-list.

    `stale_value` is the profile's own currently saved explicit value
    when editing, added to the list even if the device no longer
    offers it. That is not "let it validate" - `_validate_profile_form`
    only ever accepts a value that is still in the live list, so a
    stale one resubmitted unchanged is still rejected with a visible
    error. It only makes sure the value the form pre-selects is always
    one of the selector's own options, so there is never a suggested
    value outside the option list whose handling depends on frontend
    behaviour this integration cannot control.
    """
    values = [DO_NOT_CHANGE, *options]
    if stale_value is not None and stale_value not in values:
        values.append(stale_value)
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=values,
            translation_key="battery_profile_current",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _profile_form_schema(
    live_currents: dict[str, list[str]], stale_currents: dict[str, str] | None = None
) -> vol.Schema:
    stale_currents = stale_currents or {}
    fields: dict[Any, Any] = {vol.Required(_PROFILE_NAME): str}
    for field in PROFILE_FIELD_ORDER:
        if field in _VOLTAGE_SETTING_BY_FIELD:
            # No default: an empty box must stay empty (Do not change),
            # not fall back to some other value.
            fields[vol.Optional(field)] = _voltage_selector(_VOLTAGE_SETTING_BY_FIELD[field])
        else:
            fields[vol.Required(field, default=DO_NOT_CHANGE)] = _current_selector(
                live_currents.get(field, []), stale_value=stale_currents.get(field)
            )
    return vol.Schema(fields)


def _record_to_suggested(record: dict[str, Any]) -> dict[str, Any]:
    suggested: dict[str, Any] = {_PROFILE_NAME: record.get(_PROFILE_NAME, "")}
    for field in PROFILE_FIELD_ORDER:
        value = record.get(field)
        if field in _VOLTAGE_SETTING_BY_FIELD:
            if value is not None:
                suggested[field] = value
        else:
            suggested[field] = DO_NOT_CHANGE if value is None else str(int(value))
    return suggested


def _pair_identical(fields: dict[str, Any], other: dict[str, Any]) -> bool:
    """True when both records pin the same set of fields to the same
    values.

    Such a pair is functionally one profile under two names: no device
    state can tell them apart and applying either does the same thing.
    Two profiles that merely *can* match the same state at once are a
    different matter and are allowed - a profile setting only currents
    and a profile setting only voltages have no field explicit on both
    sides at all, so refusing that pair would make the currents-only
    profile impossible to save next to any voltage-only one. When
    several profiles do match at once, the profile select reports no
    selection and logs which ones overlapped.
    """
    return all(fields.get(field) == other.get(field) for field in PROFILE_FIELD_ORDER)


def _validate_profile_form(
    user_input: dict[str, Any],
    *,
    other_profiles: list[dict[str, Any]],
    editing_id: str | None,
    live_currents: dict[str, list[str]],
) -> tuple[dict[str, Any] | None, dict[str, str], dict[str, str]]:
    """Validate one Add/Edit submission.

    Chain ordering between two "do not change" fields, or between a
    do-not-change and an explicit field, is not checked here - either
    side can still resolve to any live value at Apply time, which this
    stage does not implement. But two *explicit* voltage values are both
    fixed by the profile itself, so their relative order is already
    knowable now; deferring that particular pair to Apply only serves
    the user a refusal after they think they already saved a valid
    profile. Otherwise this only checks what a profile record can decide
    on its own: name, each field's own documented range, live current
    membership, non-emptiness and duplication of another stored profile.
    """
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}

    name = str(user_input.get(_PROFILE_NAME, "")).strip()
    if not name:
        errors[_PROFILE_NAME] = "profile_name_required"
    elif len(name) > PROFILE_NAME_MAX_LENGTH:
        errors[_PROFILE_NAME] = "profile_name_too_long"
        placeholders["max_length"] = str(PROFILE_NAME_MAX_LENGTH)
    elif any(p[_PROFILE_NAME].casefold() == name.casefold() for p in other_profiles):
        errors[_PROFILE_NAME] = "profile_name_duplicate"

    fields: dict[str, float | int | None] = {}
    for field in PROFILE_FIELD_ORDER:
        if field in _VOLTAGE_SETTING_BY_FIELD:
            raw_value = user_input.get(field)
            if raw_value is None:
                fields[field] = None
                continue
            value = round(float(raw_value), 1)
            setting = _VOLTAGE_SETTING_BY_FIELD[field]
            lo, hi = setting.doc_range
            is_sentinel = setting.allow_zero_sentinel and value == 0.0
            if is_sentinel or lo <= value <= hi:
                fields[field] = value
            else:
                errors[field] = f"profile_{field}_range"
                placeholders[f"{field}_min"] = f"{lo:.1f}"
                placeholders[f"{field}_max"] = f"{hi:.1f}"
                fields[field] = None
        else:
            raw_value = user_input.get(field, DO_NOT_CHANGE)
            if raw_value == DO_NOT_CHANGE:
                fields[field] = None
            elif raw_value in live_currents.get(field, []):
                fields[field] = int(raw_value)
            else:
                errors[field] = "profile_current_unsupported"
                fields[field] = None

    if not errors:
        ordered_values = [
            (
                field,
                fields[field],
                _VOLTAGE_SETTING_BY_FIELD[field].allow_zero_sentinel,
                _VOLTAGE_CHAIN_GROUP_BY_PARAM[_VOLTAGE_SETTING_BY_FIELD[field].param_name],
            )
            for field in _VOLTAGE_CHAIN_FIELD_ORDER
        ]
        conflict = first_out_of_order_pair(ordered_values)
        if conflict is not None:
            lower_field, lower_value, upper_field, upper_value = conflict
            # The message carries the two values only - it renders under
            # both offending boxes at once, and those boxes already show
            # their own localized labels.
            errors[lower_field] = "profile_voltage_order_conflict"
            errors[upper_field] = "profile_voltage_order_conflict"
            placeholders["lower_value"] = f"{lower_value:.1f}"
            placeholders["upper_value"] = f"{upper_value:.1f}"

    if not errors and all(fields[f] is None for f in PROFILE_FIELD_ORDER):
        errors["base"] = "profile_all_do_not_change"

    if not errors:
        for other in other_profiles:
            if _pair_identical(fields, other):
                errors["base"] = "profile_identical_to_existing"
                placeholders["other_profile"] = other[_PROFILE_NAME]
                break

    if errors:
        return None, errors, placeholders

    record: dict[str, Any] = {_PROFILE_ID: editing_id or uuid.uuid4().hex, _PROFILE_NAME: name}
    record.update(fields)
    return record, {}, {}


class OptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Advanced-settings gate plus battery profile management.

    Every step merges into entry.options rather than replacing it: the
    advanced flag and the profile list are independent halves of the
    same options dict, so neither step can wipe the other's data. Uses
    OptionsFlowWithReload because saving either half must reload the
    entry - the select/number platforms only exist while advanced
    settings are on, and a later Apply (not in this stage) needs the
    settings coordinator to have the current profile list.
    """

    async def async_step_init(self, user_input: dict | None = None):
        container = _profile_container(self.config_entry.options)
        usable = _usable_profiles(container)
        menu_options = ["advanced_settings", "add_profile"]
        if usable:
            menu_options += ["edit_profile", "delete_profile"]
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    def _save(self, **overrides: Any):
        options = dict(self.config_entry.options)
        options.update(overrides)
        return self.async_create_entry(data=options)

    async def async_step_advanced_settings(self, user_input: dict | None = None):
        if user_input is not None:
            return self._save(**{CONF_ADVANCED_SETTINGS: user_input[CONF_ADVANCED_SETTINGS]})

        return self.async_show_form(
            step_id="advanced_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADVANCED_SETTINGS,
                        default=self.config_entry.options.get(
                            CONF_ADVANCED_SETTINGS, DEFAULT_ADVANCED_SETTINGS
                        ),
                    ): bool
                }
            ),
        )

    async def async_step_add_profile(self, user_input: dict | None = None):
        return await self._show_profile_form("add_profile", editing_id=None, user_input=user_input)

    async def async_step_edit_profile(self, user_input: dict | None = None):
        container = _profile_container(self.config_entry.options)
        if not _is_current_schema(container):
            return self.async_abort(reason="battery_profile_storage_unrecognized")
        usable = _usable_profiles(container)

        if user_input is not None:
            self._pending_profile_id = user_input["profile_id"]
            return await self.async_step_edit_profile_form()

        return self.async_show_form(
            step_id="edit_profile",
            data_schema=vol.Schema(
                {vol.Required("profile_id"): vol.In({p[_PROFILE_ID]: p[_PROFILE_NAME] for p in usable})}
            ),
        )

    async def async_step_edit_profile_form(self, user_input: dict | None = None):
        editing_id = getattr(self, "_pending_profile_id", None)
        if editing_id is None:
            return await self.async_step_init()
        return await self._show_profile_form("edit_profile_form", editing_id=editing_id, user_input=user_input)

    async def async_step_delete_profile(self, user_input: dict | None = None):
        container = _profile_container(self.config_entry.options)
        if not _is_current_schema(container):
            return self.async_abort(reason="battery_profile_storage_unrecognized")
        usable = _usable_profiles(container)

        if user_input is not None:
            self._pending_profile_id = user_input["profile_id"]
            return await self.async_step_delete_profile_confirm()

        return self.async_show_form(
            step_id="delete_profile",
            data_schema=vol.Schema(
                {vol.Required("profile_id"): vol.In({p[_PROFILE_ID]: p[_PROFILE_NAME] for p in usable})}
            ),
        )

    async def async_step_delete_profile_confirm(self, user_input: dict | None = None):
        editing_id = getattr(self, "_pending_profile_id", None)
        container = _profile_container(self.config_entry.options)
        profile = next(
            (p for p in _usable_profiles(container) if p[_PROFILE_ID] == editing_id), None
        )
        if profile is None:
            return await self.async_step_init()

        if user_input is not None:
            self._pending_profile_id = None
            if user_input.get("confirm"):
                return self._save(**{CONF_BATTERY_PROFILES: _remove_profile(container, profile[_PROFILE_ID])})
            return await self.async_step_init()

        return self.async_show_form(
            step_id="delete_profile_confirm",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            description_placeholders={"name": profile[_PROFILE_NAME]},
        )

    async def _show_profile_form(self, step_id: str, *, editing_id: str | None, user_input: dict | None):
        container = _profile_container(self.config_entry.options)
        if not _is_current_schema(container):
            return self.async_abort(reason="battery_profile_storage_unrecognized")

        usable = _usable_profiles(container)
        others = [p for p in usable if p[_PROFILE_ID] != editing_id]
        current = next((p for p in usable if p[_PROFILE_ID] == editing_id), None) if editing_id else None

        live_currents = {
            field: await _live_current_options(self.hass, self.config_entry, field)
            for field in _CURRENT_SETTING_BY_FIELD
        }

        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            record, errors, placeholders = _validate_profile_form(
                user_input, other_profiles=others, editing_id=editing_id, live_currents=live_currents
            )
            if record is not None:
                self._pending_profile_id = None
                new_container = _replace_profile(container, record, editing_id)
                return self._save(**{CONF_BATTERY_PROFILES: new_container})

        stale_currents = {
            field: str(int(current[field]))
            for field in _CURRENT_SETTING_BY_FIELD
            if current is not None and current.get(field) is not None
        }
        schema = _profile_form_schema(live_currents, stale_currents)
        if user_input is not None:
            suggested = user_input
        elif current is not None:
            suggested = _record_to_suggested(current)
        else:
            suggested = {}
        schema = self.add_suggested_values_to_schema(schema, suggested)

        return self.async_show_form(
            step_id=step_id, data_schema=schema, errors=errors, description_placeholders=placeholders
        )


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidHost(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid hostname."""
