from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_ADVANCED_SETTINGS,
    CONF_DEVICE_KEY,
    CONF_DEVICE_KEY_SOURCE,
    DEFAULT_ADVANCED_SETTINGS,
    DOMAIN,
)
from .hub import Hub, generate_device_key, normalize_serial

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


class OptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Toggle the 'advanced settings' gate for the risky tier-2 controls.

    Uses OptionsFlowWithReload so a changed value reloads the entry on its
    own - the select/number platforms only create tier-2 entities while the
    option is on, so a reload is the only way that set of entities can
    change without a restart.
    """

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
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


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidHost(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid hostname."""
