"""Platform for switch integration."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

import logging
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add switches for passed config_entry in HA."""
    hub = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities(TimeSyncSwitch(inverter) for inverter in hub.inverters)


class TimeSyncSwitch(SwitchEntity):
    """Switch that pushes the current HA date/time to the inverter."""

    should_poll = False
    _state = False
    _attr_icon = "mdi:timer-sync-outline"

    def __init__(self, inverter) -> None:
        """Initialize the switch."""
        self._inverter = inverter
        self._attr_unique_id = f"{self._inverter.inverter_id}_time_sync"
        self._attr_name = "Sync inverter Date / Time with HA"

    @property
    def device_info(self) -> DeviceInfo:
        """Information about this entity/device."""
        return self._inverter.device_info

    @property
    def available(self) -> bool:
        """Return True if inverter and hub is available."""
        return self._inverter.online and self._inverter.hub.online

    @property
    def is_on(self):
        """Return is_on status."""
        return self._state

    async def async_turn_on(self):
        """Turn On method."""

        time_sync = await self._inverter.hub.async_run(
            self._inverter.hub.inverter_client.set_current_time
        )
        if(time_sync):
            _LOGGER.info(
                "Inverter time sync successfull"
            )
        else:
            _LOGGER.error(
                "Inverter time sync faild"
            )
        self._state = True
        self.schedule_update_ha_state()

    async def async_turn_off(self):
        """Turn Off method."""
        self._state = False
        self.schedule_update_ha_state()
