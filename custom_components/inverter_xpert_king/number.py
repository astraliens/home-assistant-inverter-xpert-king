"""Platform for number integration: the battery voltage thresholds."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .battery_chain import STEP as _STEP, neighbor_bounds
from .const import CONF_ADVANCED_SETTINGS, DEFAULT_ADVANCED_SETTINGS, DOMAIN
from .entity_gate import set_gated_entity_disabled
from .entity_naming import assign_entity_id


def _describe_unconfirmed_write(label: str, result) -> str:
    """Wording for a write whose confirming QPIRI read failed - `result`
    is a `hub.SettingWriteResult` with `read_back` None. Matches the
    wording `ApplyOutcome.TRANSPORT_UNKNOWN` settled on in
    battery_apply.py: never assert the setting *was* changed, only that
    its resulting state is unknown, naming the setting and the last
    value actually confirmed for it.
    """
    if not result.sent:
        return f"could not read the inverter's current settings - {label} was not changed"
    return (
        f"a command changing {label} may have reached the inverter, but no confirming "
        f"reply came back afterward, so the resulting state is unknown; {label} was last "
        f"confirmed at {result.before!r}"
    )


@dataclass(frozen=True)
class VoltageSetting:
    """One QPIRI-backed voltage threshold, ordered against its neighbours.

    `lower_neighbor`/`upper_neighbor` name the QPIRI field (if any) that
    this one must stay above/below. Ordering is enforced here, not by the
    device - no document describes what the firmware does with an
    inverted threshold, so the interface has to keep them sane on its
    own without ever excluding the value the device is currently
    reporting (see `_bounds`).
    """

    command: str
    param_name: str
    doc_range: tuple[float, float]
    lower_neighbor: str | None
    upper_neighbor: str | None
    allow_zero_sentinel: bool = False


# All tier 2 (advanced settings only). Two independent ordered groups
# (D047) - no relation between them, even though both live in this one
# tuple and both feed the same planner/bounds/save-check machinery:
#
#   under-voltage cutoff < recharge (back to grid) < re-discharge
#   (back to battery) - a grid/battery hysteresis chain, floor for
#   re-discharge is its own documented range plus staying above
#   recharge, nothing above it;
#
#   float < bulk/CV - float is the CV-stage hold level, capped by bulk.
#
# A single flat "ascending chain order" comment used to describe this
# tuple before the live device proved PBDV can sit above both float and
# bulk at once (K79): PBDV's own upper link and PBFT's own lower link
# below are what actually define the two groups, not this tuple's
# position order, and `battery_chain.chain_groups` derives its result
# from exactly those links.
VOLTAGE_SETTINGS: tuple[VoltageSetting, ...] = (
    VoltageSetting(
        command="PSDV",
        param_name="battery_under_voltage",
        doc_range=(40.0, 54.0),
        lower_neighbor=None,
        upper_neighbor="battery_recharge_voltage",
    ),
    VoltageSetting(
        command="PBCV",
        param_name="battery_recharge_voltage",
        doc_range=(44.0, 57.0),
        lower_neighbor="battery_under_voltage",
        upper_neighbor="battery_redischarge_voltage",
    ),
    VoltageSetting(
        command="PBDV",
        param_name="battery_redischarge_voltage",
        doc_range=(48.0, 64.0),
        lower_neighbor="battery_recharge_voltage",
        upper_neighbor=None,
        allow_zero_sentinel=True,
    ),
    VoltageSetting(
        command="PBFT",
        param_name="battery_float_voltage",
        doc_range=(48.0, 64.0),
        lower_neighbor=None,
        upper_neighbor="battery_bulk_voltage",
    ),
    VoltageSetting(
        command="PCVV",
        param_name="battery_bulk_voltage",
        doc_range=(48.0, 64.0),
        lower_neighbor="battery_float_voltage",
        upper_neighbor=None,
    ),
)


def _unique_id(inverter, param_name: str) -> str:
    return f"{inverter.inverter_id}_setting_{param_name}"


def _coordinator_field(coordinator, param_name: str):
    for item in coordinator.data or ():
        if item.get("param") == param_name:
            return item.get("value")
    return None


def _format_voltage(value: float) -> str:
    """"nn.n", zero-padded so 0.0 becomes the documented "00.0" sentinel."""
    return f"{value:04.1f}"


def voltage_bounds(
    doc_range: tuple[float, float],
    current: float | None,
    lower_neighbor_value: float | None,
    upper_neighbor_value: float | None,
) -> tuple[float, float]:
    """Return (min, max) for one threshold, constrained by its neighbours
    but never excluding the value the device is currently reporting.

    The neighbour-tightened range itself comes from `battery_chain` -
    the same rules the profile-apply planner uses to check a step. This
    wrapper adds the one behaviour that is specific to a live UI control:
    if the neighbour-derived range would be empty (lo > hi) or would
    exclude the current live value - i.e. the device already reports
    something in a violated order - the whole constraint is dropped for
    this refresh and the full documented range is returned instead, so
    the interface never becomes unable to show or reselect what the
    device already has.
    """
    lo, hi = neighbor_bounds(doc_range, lower_neighbor_value, upper_neighbor_value)
    if lo > hi or (current is not None and not (lo <= current <= hi)):
        return doc_range
    return (lo, hi)


def _new_voltage_entities(hass, coordinator, inverter, settings, known: set[str]):
    """Create number entities for settings the device has reported and not
    yet turned into an entity - called again on every later 'conf' poll."""
    entities = []
    for setting in settings:
        if setting.param_name in known:
            continue
        if _coordinator_field(coordinator, setting.param_name) is None:
            continue
        known.add(setting.param_name)
        entities.append(InverterVoltageNumber(hass, coordinator, inverter, setting))
    return entities


def _reconcile_number_gate(hass: HomeAssistant, inverter, *, advanced: bool) -> None:
    """Apply the advanced-settings gate to every number unique_id we own."""
    for setting in VOLTAGE_SETTINGS:
        set_gated_entity_disabled(hass, "number", _unique_id(inverter, setting.param_name), not advanced)


def _watch_for_new_voltage_entities(
    hass, coordinator, inverter, settings, known, async_add_entities, config_entry, *, advanced: bool
):
    @callback
    def _on_update() -> None:
        new_entities = _new_voltage_entities(hass, coordinator, inverter, settings, known)
        if new_entities:
            # Clear a removed-row integration disable before HA restores it.
            _reconcile_number_gate(hass, inverter, advanced=advanced)
            async_add_entities(new_entities)

    unsub = coordinator.async_add_listener(_on_update)
    config_entry.async_on_unload(unsub)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add number entities for passed config_entry in HA."""
    hub = hass.data[DOMAIN][config_entry.entry_id]
    advanced = config_entry.options.get(CONF_ADVANCED_SETTINGS, DEFAULT_ADVANCED_SETTINGS)

    entities: list[NumberEntity] = []
    inverters = list(hub.inverters)

    for inverter in inverters:
        # All voltage thresholds are tier 2 - see select.py's setup for the
        # same gating logic (disable rather than delete while off).
        # Reconcile before registration so a removed row still carrying
        # disabled_by=integration is cleared before HA restores it.
        _reconcile_number_gate(hass, inverter, advanced=advanced)

        if not advanced:
            continue

        coordinator = hub.settings_coordinator
        known: set[str] = set()
        entities.extend(_new_voltage_entities(hass, coordinator, inverter, VOLTAGE_SETTINGS, known))
        _watch_for_new_voltage_entities(
            hass,
            coordinator,
            inverter,
            VOLTAGE_SETTINGS,
            known,
            async_add_entities,
            config_entry,
            advanced=advanced,
        )

    if entities:
        async_add_entities(entities)

    # Second pass for rows that were already live (not in the deleted area).
    for inverter in inverters:
        _reconcile_number_gate(hass, inverter, advanced=advanced)


class InverterVoltageNumber(CoordinatorEntity, NumberEntity):
    """Number backed by one QPIRI voltage field on the shared settings
    coordinator.

    State always comes from the device's own answer: the write path
    re-reads QPIRI and shows whatever it actually says (raising on
    refusal or mismatch), and every later coordinator refresh that
    carries the field keeps it in step with a change made on the panel.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "V"
    _attr_device_class = NumberDeviceClass.VOLTAGE
    _attr_mode = NumberMode.BOX
    _attr_native_step = _STEP

    def __init__(self, hass, coordinator, inverter, setting: VoltageSetting) -> None:
        super().__init__(coordinator)
        self._inverter = inverter
        self._setting = setting
        self._attr_unique_id = _unique_id(inverter, setting.param_name)
        self._attr_translation_key = setting.param_name
        self._state = _coordinator_field(coordinator, setting.param_name)
        assign_entity_id(hass, self, "number", setting.param_name)

    @property
    def device_info(self) -> DeviceInfo:
        return self._inverter.device_info

    @property
    def available(self) -> bool:
        return self._inverter.online and self._inverter.hub.online

    @property
    def native_value(self) -> float | None:
        return self._state

    def _bounds(self) -> tuple[float, float]:
        current = _coordinator_field(self.coordinator, self._setting.param_name)
        lower_val = (
            _coordinator_field(self.coordinator, self._setting.lower_neighbor)
            if self._setting.lower_neighbor
            else None
        )
        upper_val = (
            _coordinator_field(self.coordinator, self._setting.upper_neighbor)
            if self._setting.upper_neighbor
            else None
        )
        return voltage_bounds(self._setting.doc_range, current, lower_val, upper_val)

    @property
    def native_min_value(self) -> float:
        if self._setting.allow_zero_sentinel:
            return 0.0
        return self._bounds()[0]

    @property
    def native_max_value(self) -> float:
        return self._bounds()[1]

    @property
    def extra_state_attributes(self) -> dict | None:
        if self._setting.allow_zero_sentinel and self._state == 0.0:
            return {
                "sentinel": (
                    "0.0 V means the device stays on battery until it is "
                    "fully charged before switching back to discharge"
                )
            }
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        value = _coordinator_field(self.coordinator, self._setting.param_name)
        if value is not None:
            self._state = value
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        rounded = round(value, 1)
        label = self._setting.param_name.replace('_', ' ')
        is_sentinel = self._setting.allow_zero_sentinel and rounded == 0.0
        if not is_sentinel:
            lo, hi = self._bounds()
            if not (lo <= rounded <= hi):
                if self._setting.allow_zero_sentinel:
                    raise HomeAssistantError(
                        f"{rounded:.1f} V is not a valid value for {label}: only exactly "
                        f"0.0 V (stay on battery until full) or {lo:.1f}-{hi:.1f} V are accepted"
                    )
                raise HomeAssistantError(
                    f"{rounded:.1f} V is outside the accepted {lo:.1f}-{hi:.1f} V range for {label}"
                )
        param = _format_voltage(rounded)
        result = await self._inverter.hub.async_write_qpiri_setting(
            self._setting.command, param, self._setting.param_name, rounded
        )
        if not result.ok:
            if result.read_back is None:
                raise HomeAssistantError(_describe_unconfirmed_write(label, result))
            raise HomeAssistantError(
                f"Inverter would not set {label} to {rounded:.1f} V "
                f"(device now reports {result.read_back!r})"
            )
        self._state = result.read_back
        self.schedule_update_ha_state()
