"""Platform for sensor integration."""
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ORPHAN_ABSENCE_THRESHOLD
from .entity_naming import CONNECTION_ENTITY_KEY, assign_entity_id, connection_target
from .inverter.qflag import decode_flag_status
from .inverter.qpiws import decode_warning_status

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add sensors for passed config_entry in HA."""
    hub = hass.data[DOMAIN][config_entry.entry_id]

    new_devices = []
    for inverter in hub.inverters:
        # hub.data_coordinator was already created and first-refreshed in
        # __init__.py, before any platform was set up - it's shared with
        # the switch platform's own QFLAG-based discovery, so setup here
        # only attaches to it, it never polls on its own account.
        coordinator = hub.data_coordinator
        coordinator.absence_tracker = FieldAbsenceTracker(hass, inverter.inverter_id)
        if coordinator.last_update_success:
            coordinator.absence_tracker.observe(coordinator.last_command_defs, coordinator.last_results)

        # hub.settings_coordinator was already created and first-refreshed
        # in __init__.py, the same way as hub.data_coordinator above - the
        # select and number platforms read the same instance, so setup
        # here only attaches to it, it never polls QPIRI on its own.
        coordinator_diag = hub.settings_coordinator
        coordinator_diag.absence_tracker = FieldAbsenceTracker(hass, inverter.inverter_id)
        if coordinator_diag.last_update_success:
            coordinator_diag.absence_tracker.observe(
                coordinator_diag.last_command_defs, coordinator_diag.last_results
            )

        known_data_params = set()
        new_devices += _build_entities(
            hass, coordinator, inverter, coordinator.data or [], known_data_params
        )

        new_devices.append(SensorInverterBatteryCurrent(hass, coordinator, inverter, []))
        new_devices.append(SensorInverterBatteryPower(hass, coordinator, inverter, []))
        new_devices.append(SensorInverterBatteryPowerCharging(hass, coordinator, inverter, []))
        new_devices.append(SensorInverterBatteryPowerDischarge(hass, coordinator, inverter, []))

        known_conf_params = set()
        new_devices += _build_entities(
            hass,
            coordinator_diag,
            inverter,
            coordinator_diag.data or [],
            known_conf_params,
            diag=True,
        )

        # Connection endpoint — not a coordinator entity and not a wire
        # field, so it stays available when the inverter is unreachable
        # and the absence tracker cannot disable it.
        new_devices.append(SensorInverterConnection(hass, inverter))

        # A field missing from this first poll isn't gone for good — every
        # later poll gets the same chance to introduce it.
        _watch_for_new_params(
            hass, coordinator, inverter, known_data_params, async_add_entities, config_entry
        )
        _watch_for_new_params(
            hass,
            coordinator_diag,
            inverter,
            known_conf_params,
            async_add_entities,
            config_entry,
            diag=True,
        )

    if new_devices:
        async_add_entities(new_devices)


def _described_param_names(field_conf) -> list[str]:
    """Field names a command-table entry actually produces once parsed.

    A plain field's own `name` is the param name. A bitfield
    (`string_binary`/`assoc_value`) decodes into one entity per entry in
    `complex_params`, under those entries' own names - the outer `name` is
    never emitted by `process_data` for those types.
    """
    if field_conf.get("type") in ("string_binary", "assoc_value"):
        return [p["name"] for p in field_conf.get("complex_params", {}).get("params", [])]
    return [field_conf["name"]]


class FieldAbsenceTracker:
    """Disables a sensor once its field, or its whole command, has
    genuinely stopped coming back.

    Counts consecutive *successfully parsed* polls of a command where a
    described field was missing. A failed poll (timeout, bad frame, bad
    CRC) proves nothing about whether the field is supported, so it
    doesn't move the counter either way.

    A stable run of NAKs for a command is a different kind of evidence:
    the device is actively refusing that command, not just failing to
    answer it, which is a reasonable sign the command itself isn't
    implemented on this model. That disables every entity the command
    feeds, using the same streak length as field absence. A later
    non-NAK successful reply for the command clears the streak and lifts
    the disable - a flaky moment must not bury the entities for good. The
    command keeps being polled either way; only its entities' enabled
    state is affected.

    Only entities this tracker itself disabled are ever re-enabled
    automatically; an entity the user disabled is left alone.
    """

    def __init__(self, hass, device_key: str):
        self._hass = hass
        self._device_key = device_key
        self._absent_streak: dict[str, int] = {}
        self._nak_streak: dict[str, int] = {}

    def observe(self, command_defs, results) -> None:
        """command_defs: this poll group's slice of the command table.
        results: iterable of one poll's outcome per command, either
        (command, ok, parsed_fields), (command, ok, parsed_fields,
        failed_fields) or (command, ok, parsed_fields, failed_fields,
        naked) - shorter forms are treated as having no parse failures
        and no NAK, so callers that don't track those still work."""
        for result in results:
            command, ok, parsed, *rest = result
            failed = rest[0] if rest else ()
            naked = rest[1] if len(rest) > 1 else False

            if not ok:
                if naked:
                    streak = self._nak_streak.get(command, 0) + 1
                    self._nak_streak[command] = streak
                    if streak >= ORPHAN_ABSENCE_THRESHOLD:
                        self._set_command_disabled(command_defs, command, True)
                continue

            nak_streak = self._nak_streak.pop(command, 0)
            if nak_streak >= ORPHAN_ABSENCE_THRESHOLD:
                self._set_command_disabled(command_defs, command, False)

            described = set()
            for field_conf in command_defs.get(command, {}).values():
                described.update(_described_param_names(field_conf))
            present = {item["param"] for item in parsed}
            for param in described:
                if param in present:
                    had_streak = self._absent_streak.pop(param, 0) > 0
                    if had_streak:
                        self._set_disabled(param, False)
                elif param in failed:
                    # Arrived but failed to convert - that says nothing
                    # about whether the device supports it, so it must not
                    # advance (or reset) the absence streak either way.
                    continue
                else:
                    streak = self._absent_streak.get(param, 0) + 1
                    self._absent_streak[param] = streak
                    if streak >= ORPHAN_ABSENCE_THRESHOLD:
                        self._set_disabled(param, True)

    def _set_command_disabled(self, command_defs, command, disabled: bool) -> None:
        """Apply a disable/enable to every field a NAKed command feeds."""
        for field_conf in command_defs.get(command, {}).values():
            for param in _described_param_names(field_conf):
                self._set_disabled(param, disabled)

    def _set_disabled(self, param: str, disabled: bool) -> None:
        registry = er.async_get(self._hass)
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{self._device_key}_{param}")
        if entity_id is None:
            return
        entry = registry.entities.get(entity_id)
        if entry is None:
            return
        if disabled:
            if entry.disabled_by is not None:
                return  # already disabled, whether by us or the user
            registry.async_update_entity(
                entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
        else:
            if entry.disabled_by != er.RegistryEntryDisabler.INTEGRATION:
                return  # never lift a disable the user set themselves
            registry.async_update_entity(entity_id, disabled_by=None)


class SensorBase(CoordinatorEntity, SensorEntity):
    """Common properties for an inverter-backed sensor entity."""

    _attr_device_class = None
    _attr_state_class = None

    def __init__(self, coordinator, inverter, inverter_param):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.hub = coordinator.hub
        self.inverter_param = inverter_param
        self._inverter = inverter
        self.param_name=inverter_param['param']

    @property
    def device_info(self):
        """Return information to link this entity with the correct device."""
        return self._inverter.device_info

    def _connection_ok(self) -> bool:
        return self._inverter.online and self._inverter.hub.online

    @property
    def available(self) -> bool:
        """Return True if inverter and hub is available."""
        return self._connection_ok()


class SensorInverterConnection(SensorEntity):
    """Shows the configured connection endpoint (host:port today).

    Not backed by a coordinator poll — availability must not follow the
    inverter link, because that is the situation this sensor exists to
    explain. Reconfigure reloads the entry, so a new address appears
    without a separate update path.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = CONNECTION_ENTITY_KEY
    _attr_should_poll = False

    def __init__(self, hass, inverter) -> None:
        self._inverter = inverter
        self._attr_unique_id = f"{inverter.inverter_id}_{CONNECTION_ENTITY_KEY}"
        assign_entity_id(hass, self, "sensor", CONNECTION_ENTITY_KEY)

    @property
    def device_info(self):
        return self._inverter.device_info

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return connection_target(self._inverter.hub)


class SensorInverter(SensorBase):
    # Floor for suggested_display_precision when the wire token has fewer
    # (or no) fractional digits. None = not a numeric display (info/text).
    _precision_floor: int | None = None

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(coordinator, inverter, inverter_param)
        self.sensor_name=inverter_param["param"]
        self._attr_unique_id = f"{self._inverter.inverter_id}_{inverter_param['param']}"
        self._attr_name = f"{inverter_param['text']}"
        self._attr_native_unit_of_measurement = inverter_param["unit"] or None
        self._missing_params_logged = set()
        # `diag` is only ever set to the boolean True by _build_entities.
        if inverter_param.get("diag"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        assign_entity_id(hass, self, "sensor", inverter_param["param"])

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    def GetParamDataByName(self, param_name):
        """Return the poll result for a field, or None if it's absent this cycle."""
        for data_item in self.coordinator.data or ():
            if data_item['param'] == param_name:
                self._missing_params_logged.discard(param_name)
                return data_item

        if param_name not in self._missing_params_logged:
            self._missing_params_logged.add(param_name)
            _LOGGER.debug("%s: field %r missing from this poll", self._attr_unique_id, param_name)
        return None

    def _values(self, *param_names):
        """Return the data items for all names, or None if any is missing."""
        items = [self.GetParamDataByName(name) for name in param_names]
        if any(item is None for item in items):
            return None
        return items

    @property
    def available(self) -> bool:
        """A sensor whose field didn't come back this cycle is unavailable, not broken."""
        return self._connection_ok() and self.GetParamDataByName(self.sensor_name) is not None

    @property
    def native_value(self):
        data_item = self.GetParamDataByName(self.sensor_name)
        return data_item["value"] if data_item is not None else None

    @property
    def suggested_display_precision(self) -> int | None:
        """Prefer the device's own fractional digits; never below the quantity floor.

        Presentation only - stored/native values are left alone. When the
        wire token carried more decimals than the floor (battery voltage
        often arrives as nn.nn), keep that resolution so HA does not paint
        a fractional reading as a bare integer. Looks the field up quietly
        so a derived sensor (no matching poll field) does not log absence.
        """
        floor = self._precision_floor
        if floor is None:
            return None
        for data_item in self.coordinator.data or ():
            if data_item.get("param") == self.sensor_name:
                wire = data_item.get("decimals")
                if isinstance(wire, int) and wire >= 0:
                    return max(wire, floor)
                break
        return floor

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return if the entity should be enabled when first added to the entity registry."""
        disabled_params=['machine_type','output_mode','pv_power_balance','pv_ok_condition_for_parallel']
        # Hide fast changing status sensors
        if self.param_name in (disabled_params):
            return False
        return True

class SensorInverterVoltage(SensorInverter):
    _precision_floor = 1

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.VOLTAGE

class SensorInverterCurrent(SensorInverter):
    _precision_floor = 1

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.CURRENT

class SensorInverterFrequency(SensorInverter):
    _precision_floor = 1

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.FREQUENCY

class SensorInverterPower(SensorInverter):
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.POWER

class SensorInverterApparentPower(SensorInverter):
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.APPARENT_POWER

class SensorInverterPercent(SensorInverter):
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = PERCENTAGE

class SensorInverterBattery(SensorInverter):
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_native_unit_of_measurement = PERCENTAGE

class SensorInverterTemperature(SensorInverter):
    _precision_floor = 1

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.TEMPERATURE

class SensorInverterDuration(SensorInverter):
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.DURATION

class SensorInverterInfo(SensorInverter):
    """Text/status field with no numeric meaning — no device or state class, just a string state."""

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)


class SensorInverterWarningStatus(SensorInverterInfo):
    """QPIWS boiled down to a health summary: ok / warning / fault."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "warning", "fault", "unknown"]
    _attr_translation_key = "warning_status"

    @property
    def native_value(self):
        data_item = self.GetParamDataByName(self.sensor_name)
        if data_item is None:
            return None
        return decode_warning_status(data_item["value"]).status

    @property
    def extra_state_attributes(self):
        data_item = self.GetParamDataByName(self.sensor_name)
        if data_item is None:
            return None
        decoded = decode_warning_status(data_item["value"])
        return {
            "summary": decoded.summary,
            "faults": decoded.faults,
            "warnings": decoded.warnings,
            "info": decoded.info,
            "fault_code": decoded.fault_code,
            "fault_description": decoded.fault_description,
            "unknown_bits": decoded.unknown_bits,
            "raw": decoded.raw,
            "bits_length": decoded.bits_length,
        }


class SensorInverterFlagStatus(SensorInverterInfo):
    """QFLAG boiled down to a list of enabled device settings."""

    @property
    def native_value(self):
        data_item = self.GetParamDataByName(self.sensor_name)
        if data_item is None:
            return None
        return decode_flag_status(data_item["value"]).state

    @property
    def extra_state_attributes(self):
        data_item = self.GetParamDataByName(self.sensor_name)
        if data_item is None:
            return None
        decoded = decode_flag_status(data_item["value"])
        return {
            "enabled": decoded.enabled,
            "disabled": decoded.disabled,
            "not_reported": decoded.not_reported,
            "unknown_letters": decoded.unknown_letters,
            "flags": decoded.flags,
            "battery_connected": decoded.battery_connected,
            "raw": decoded.raw,
        }


class SensorInverterEnergy(SensorInverter):
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.ENERGY

class SensorInverterEnergyTotal(SensorInverter):
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_device_class = SensorDeviceClass.ENERGY
        # TOTAL_INCREASING would mis-aggregate when zero values appear in the
        # stream before we can filter them, so keep TOTAL for now.
        self._attr_state_class = SensorStateClass.TOTAL


class SensorInverterBatteryCurrent(SensorInverter):
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _precision_floor = 1

    def __init__(self, hass, coordinator, inverter, inverter_param):
        inverter_param={'param':'battery_current','text':'Battery current','unit':'A'}
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_unique_id = f"{self._inverter.inverter_id}_{self.sensor_name}"

    @property
    def available(self) -> bool:
        return self._connection_ok() and self._values('battery_charging_current', 'battery_discharge_current') is not None

    @property
    def native_value(self):
        values = self._values('battery_charging_current', 'battery_discharge_current')
        if values is None:
            return None
        charging, discharge = values
        return charging["value"] - discharge["value"]

class SensorInverterBatteryPower(SensorInverter):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        inverter_param={'param':'battery_power','text':'Battery power','unit':'W'}
        super().__init__(hass, coordinator, inverter, inverter_param)
        self._attr_unique_id = f"{self._inverter.inverter_id}_{self.sensor_name}"

    @property
    def available(self) -> bool:
        return self._connection_ok() and self._values(
            'battery_charging_current', 'battery_discharge_current', 'battery_voltage'
        ) is not None

    @property
    def native_value(self):
        values = self._values('battery_charging_current', 'battery_discharge_current', 'battery_voltage')
        if values is None:
            return None
        charging, discharge, voltage = values
        return (charging["value"] - discharge["value"]) * voltage["value"]

class SensorInverterBatteryPowerCharging(SensorInverter):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        inverter_param={'param':'battery_power_charging','text':'Battery Power Charging','unit':'W'}
        super().__init__(hass, coordinator, inverter, inverter_param)

    @property
    def available(self) -> bool:
        return self._connection_ok() and self._values('battery_charging_current', 'battery_voltage') is not None

    @property
    def native_value(self):
        values = self._values('battery_charging_current', 'battery_voltage')
        if values is None:
            return None
        charging, voltage = values
        return charging["value"] * voltage["value"]

class SensorInverterBatteryPowerDischarge(SensorInverter):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _precision_floor = 0

    def __init__(self, hass, coordinator, inverter, inverter_param):
        inverter_param={'param':'battery_power_discharge','text':'Battery Power Discharge','unit':'W'}
        super().__init__(hass, coordinator, inverter, inverter_param)

    @property
    def available(self) -> bool:
        return self._connection_ok() and self._values('battery_discharge_current', 'battery_voltage') is not None

    @property
    def native_value(self):
        values = self._values('battery_discharge_current', 'battery_voltage')
        if values is None:
            return None
        discharge, voltage = values
        return discharge["value"] * voltage["value"]


SENSOR_CLASS_BY_TYPE = {
    "voltage": SensorInverterVoltage,
    "current": SensorInverterCurrent,
    "frequency": SensorInverterFrequency,
    "power": SensorInverterPower,
    "apparent_power": SensorInverterApparentPower,
    "energy": SensorInverterEnergy,
    "energy_total": SensorInverterEnergyTotal,
    "percent": SensorInverterPercent,
    "battery": SensorInverterBattery,
    "temperature": SensorInverterTemperature,
    "duration_time": SensorInverterDuration,
    "info": SensorInverterInfo,
    "warning_status": SensorInverterWarningStatus,
    "flag_status": SensorInverterFlagStatus,
}


def _build_entities(hass, coordinator, inverter, params, known_params, *, diag=False):
    """Create entities for params seen in a poll that aren't known yet.

    Discovery follows what the device actually returned, not the command
    table — the table is a protocol-family superset, and creating
    everything from it would add entities for fields this model never
    sends. `known_params` is shared with the caller and grows over time,
    so this can be called again on every later poll.
    """
    entities = []
    for inverter_param in params:
        param_name = inverter_param.get("param")
        entity_cls = SENSOR_CLASS_BY_TYPE.get(inverter_param.get("sensor"))
        if not param_name or entity_cls is None or param_name in known_params:
            continue
        if diag:
            inverter_param = {**inverter_param, "diag": True}
        known_params.add(param_name)
        entities.append(entity_cls(hass, coordinator, inverter, inverter_param))
    return entities


def _watch_for_new_params(
    hass, coordinator, inverter, known_params, async_add_entities, config_entry, *, diag=False
):
    """Keep adding entities as new fields show up in later polls.

    The listener must not outlive the config entry it belongs to — an
    unload (or a reload, which unloads first) has to remove it, or a
    stale callback keeps firing against a coordinator/async_add_entities
    that no longer has anywhere valid to add entities to.
    """

    @callback
    def _on_update() -> None:
        new_entities = _build_entities(
            hass, coordinator, inverter, coordinator.data or [], known_params, diag=diag
        )
        if new_entities:
            async_add_entities(new_entities)

    unsub = coordinator.async_add_listener(_on_update)
    config_entry.async_on_unload(unsub)
