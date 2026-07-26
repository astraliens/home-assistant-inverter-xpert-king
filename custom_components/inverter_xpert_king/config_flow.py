from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_KEY, CONF_DEVICE_KEY_SOURCE, DOMAIN
from .hub import Hub, generate_device_key, normalize_serial

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
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
    an existing entry (T08) changes the address, never the identity.
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
                entry_data = {
                    CONF_HOST: info["title"],
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_DEVICE_KEY: info["device_key"],
                    CONF_DEVICE_KEY_SOURCE: info["device_key_source"],
                }
                return self.async_create_entry(title=info["title"], data=entry_data)

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
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
                DATA_SCHEMA, reconfigure_entry.data
            ),
            errors=errors,
        )


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidHost(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid hostname."""
