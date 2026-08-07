"""Platform for switch integration."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .entity_naming import assign_entity_id
from .inverter.qflag import WRITABLE_LETTERS, decode_flag_status

# Letter and stable QFLAG key (inverter.qflag._FLAG_KEYS). Entity names
# come from translations keyed on the same string. Letters 'j' (power
# saving) and 'm' (battery status) stay out of this list on purpose.
_FLAG_TOGGLES = (
    ("x", "backlight"),
    ("k", "lcd_timeout"),
    ("z", "fault_log"),
    ("y", "source_alarm"),
    ("u", "overload_restart"),
    ("v", "over_temperature_restart"),
    ("b", "overload_bypass"),
    ("c", "overload_bypass_2"),
    ("a", "buzzer"),
)

# A plain `assert` is stripped by `python -O`, which would silently let
# this list and the write primitive's allow-list drift apart - this check
# has to survive that, so it is a real `if`/`raise` instead.
if {letter for letter, _key in _FLAG_TOGGLES} != WRITABLE_LETTERS:
    raise RuntimeError(
        "switch._FLAG_TOGGLES and qflag.WRITABLE_LETTERS have drifted apart - "
        "these two must list exactly the same letters"
    )


def _decode_flag_status(coordinator_data):
    """Pull QFLAG's raw string out of the shared data coordinator's parsed
    field list and decode it.

    Returns None when this poll doesn't have it - either a failed/refused
    read, or no successful poll has happened yet. `coordinator_data` is
    the same flat field list the sensor platform's entities read from
    (hub.data_coordinator.data); this never issues a read of its own.
    """
    for item in coordinator_data or ():
        if item.get("param") == "flag_status":
            return decode_flag_status(item["value"])
    return None


def _new_flag_entities(hass, coordinator, inverter, status, known_letters: set[str]):
    """Toggle entities for approved letters not yet created.

    Mirrors sensor.py's field-discovery pattern: called again on every
    later poll, so a letter missing from a failed or refused first read
    still gets its entity created once the device eventually reports it.
    `known_letters` is shared with the caller and grows over time, so a
    letter already turned into an entity is never handed back again.
    Each toggle is bound to the same shared data coordinator so later
    QFLAG polls keep its state in step with the device (and with the
    flag_status sensor) without a second reader of that command.
    """
    if status is None:
        return []
    entities = []
    for letter, key in _FLAG_TOGGLES:
        if letter in known_letters or key not in status.flags:
            continue
        known_letters.add(letter)
        entities.append(
            InverterFlagSwitch(hass, coordinator, inverter, letter, key, status.flags[key])
        )
    return entities


def _watch_for_new_flags(hass, coordinator, inverter, known_letters, async_add_entities, config_entry):
    """Ride the shared data coordinator's own polling to keep adding toggle
    entities once QFLAG becomes readable - the same shape as the sensor
    platform's field discovery, but with no timer or read of its own: it
    only ever looks at coordinator.data, which the coordinator already
    fetched for the flag_status sensor (and every other field in the
    'data' group).

    Self-unsubscribes the first time QFLAG comes back parsed at all,
    whether or not that reply had every approved letter. From that point
    on, any approved letter still missing is the documented "device
    doesn't report it" case - a single successful QFLAG reply is
    conclusive about which letters exist, so there is nothing left to
    wait for. Without this, a device that only ever reports some of the
    approved letters would be watched, uselessly, for as long as the
    config entry is loaded.

    Also registered for config_entry.async_on_unload as a safety net for
    the case QFLAG never once comes back parsed before the entry is
    unloaded or reloaded - the listener must not outlive the entry either
    way, and unsubscribing twice (once here, once from the self-stop
    above) has to be harmless.
    """
    stopped = False

    def _stop() -> None:
        nonlocal stopped
        if not stopped:
            stopped = True
            unsub()

    @callback
    def _on_update() -> None:
        if stopped:
            return
        status = _decode_flag_status(coordinator.data)
        new_entities = _new_flag_entities(hass, coordinator, inverter, status, known_letters)
        if new_entities:
            async_add_entities(new_entities)
        if status is not None:
            _stop()

    unsub = coordinator.async_add_listener(_on_update)
    config_entry.async_on_unload(_stop)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add switches for passed config_entry in HA."""
    hub = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[SwitchEntity] = []

    for inverter in hub.inverters:
        known_letters: set[str] = set()
        # hub.data_coordinator was already created and first-refreshed in
        # __init__.py, before any platform was set up - this is the exact
        # same coordinator the flag_status sensor reads from, not a
        # second poll of QFLAG.
        coordinator = hub.data_coordinator
        status = _decode_flag_status(coordinator.data)

        entities.extend(_new_flag_entities(hass, coordinator, inverter, status, known_letters))
        _watch_for_new_flags(hass, coordinator, inverter, known_letters, async_add_entities, config_entry)

    async_add_entities(entities)


class InverterFlagSwitch(CoordinatorEntity, SwitchEntity):
    """Switch backed by one QFLAG letter on the shared data coordinator.

    State always comes from the device's own answer: the write path
    re-reads QFLAG and shows whatever it actually says (raising on
    refusal or mismatch), and every later shared-coordinator refresh that
    carries a parsed QFLAG updates the toggle the same way - so a change
    made on the inverter's panel is reflected without a second poller.
    A refresh that fails or omits QFLAG leaves the last known state alone.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(
        self, hass, coordinator, inverter, letter: str, key: str, initial_state: bool
    ) -> None:
        """Initialize the switch from an already-confirmed QFLAG read."""
        super().__init__(coordinator)
        self._inverter = inverter
        self._letter = letter
        self._key = key
        self._attr_unique_id = f"{self._inverter.inverter_id}_flag_{key}"
        self._attr_translation_key = key
        self._state = initial_state
        assign_entity_id(hass, self, "switch", key)

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

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh from the shared poll when this cycle carried QFLAG."""
        status = _decode_flag_status(self.coordinator.data)
        if status is not None and self._key in status.flags:
            self._state = status.flags[self._key]
        self.async_write_ha_state()

    async def async_turn_on(self):
        """Enable this letter via PE, confirmed by re-reading QFLAG."""
        await self._set(True)

    async def async_turn_off(self):
        """Disable this letter via PD, confirmed by re-reading QFLAG."""
        await self._set(False)

    async def _set(self, enabled: bool) -> None:
        result = await self._inverter.hub.async_write_flag(self._letter, enabled)
        if not result.ok:
            action = "enable" if enabled else "disable"
            raise HomeAssistantError(
                f"Inverter would not {action} {self._key.replace('_', ' ')} "
                f"(QFLAG now reports {result.read_back!r})"
            )
        self._state = result.read_back
        self.schedule_update_ha_state()
