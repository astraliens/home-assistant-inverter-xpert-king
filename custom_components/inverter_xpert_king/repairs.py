"""Fix flows for repair issues raised by this integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from . import async_apply_advanced_settings
from .const import CONF_ADVANCED_SETTINGS, DEFAULT_ADVANCED_SETTINGS, DOMAIN


class AdvancedSettingsRepairFlow(RepairsFlow):
    """One-step offer to enable advanced settings, or decline permanently."""

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        # Flow manager may pass an empty dict on init — never treat that
        # as a submitted answer.
        return await self.async_step_offer()

    async def async_step_offer(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            enabled = bool(user_input[CONF_ADVANCED_SETTINGS])
            await async_apply_advanced_settings(self.hass, self.entry, enabled)
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="offer",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADVANCED_SETTINGS,
                        default=DEFAULT_ADVANCED_SETTINGS,
                    ): bool
                }
            ),
            description_placeholders={"title": self.entry.title},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a flow to fix a fixable issue."""
    if issue_id.startswith("advanced_settings_available_") and data:
        entry_id = data.get("entry_id")
        if isinstance(entry_id, str):
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == DOMAIN:
                return AdvancedSettingsRepairFlow(entry)
    return ConfirmRepairFlow()
