"""Platform for button integration."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity_naming import assign_entity_id


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the time-sync button for passed config_entry in HA."""
    hub = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(SyncDateTimeButton(hass, inverter) for inverter in hub.inverters)


def _time_sync_failure_reason(result) -> str:
    if not result.accepted:
        return "device refused the DAT command"
    if result.device_time is None:
        return "could not read the clock back (QT)"
    return (
        f"device clock reads {result.device_time.isoformat(sep=' ')}, "
        f"expected close to {result.target_time.isoformat(sep=' ')}"
    )


class SyncDateTimeButton(ButtonEntity):
    """Button that syncs the inverter's clock to the current HA time.

    Pressing it reads the clock first: if it is already within tolerance
    nothing is sent at all, the same redundant-write suppression the flag
    switches use. Otherwise DAT is sent and the clock is read back again -
    a refusal or a mismatch raises instead of pretending it worked. There
    is no periodic sync anywhere; the only way this ever writes to the
    device is a press.
    """

    _attr_icon = "mdi:timer-sync-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_translation_key = "sync_date_and_time"

    def __init__(self, hass, inverter) -> None:
        """Initialize the button."""
        self._inverter = inverter
        self._attr_unique_id = f"{self._inverter.inverter_id}_sync_datetime"
        assign_entity_id(hass, self, "button", "sync_datetime")

    @property
    def device_info(self) -> DeviceInfo:
        """Information about this entity/device."""
        return self._inverter.device_info

    @property
    def available(self) -> bool:
        """Return True if inverter and hub is available."""
        return self._inverter.online and self._inverter.hub.online

    async def async_press(self) -> None:
        """Sync the clock, confirmed by reading QT before and after."""
        result = await self._inverter.hub.async_sync_time()
        if not result.ok:
            raise HomeAssistantError(
                "Inverter time sync failed: " + _time_sync_failure_reason(result)
            )
