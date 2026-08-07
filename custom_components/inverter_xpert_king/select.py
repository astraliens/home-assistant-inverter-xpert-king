"""Platform for select integration: enum-shaped QPIRI settings."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADVANCED_SETTINGS, DEFAULT_ADVANCED_SETTINGS, DOMAIN
from .entity_gate import set_gated_entity_disabled
from .entity_naming import assign_entity_id


@dataclass(frozen=True)
class EnumSetting:
    """One QPIRI-backed enum control.

    `slug_by_code` maps the two-digit wire code to a stable option slug -
    the slug is the HA-facing identifier (translated per language);
    the label the device actually reports for that code is read from
    `inverter_commands.json` at setup, not duplicated here.
    """

    command: str
    param_name: str
    tier2: bool
    slug_by_code: dict[str, str]


# Tier 1 (always) and tier 2 (advanced settings only) enum controls. Codes
# and their meanings come straight from the KING set-command table, not
# the QPIRI chapter of the vendor document - the two disagree for PCP, and
# the set-command meanings are the ones that match what this device does.
ENUM_SETTINGS: tuple[EnumSetting, ...] = (
    EnumSetting(
        command="PCP",
        param_name="charger_source_priority",
        tier2=False,
        slug_by_code={"0": "sbl_ucb", "1": "sbl_udc", "2": "slb_ucb", "3": "slb_udc"},
    ),
    EnumSetting(
        command="PGR",
        param_name="input_voltage_range",
        tier2=False,
        slug_by_code={"0": "appliance", "1": "ups"},
    ),
    EnumSetting(
        command="POP",
        param_name="output_source_priority",
        tier2=True,
        slug_by_code={"0": "usb", "1": "sub", "2": "sbu"},
    ),
    EnumSetting(
        command="POPLG",
        param_name="operation_logic",
        tier2=True,
        slug_by_code={"0": "automatic", "1": "online", "2": "eco"},
    ),
    EnumSetting(
        command="PBT",
        param_name="battery_type",
        tier2=True,
        # Codes 07-09 exist in the JSON (from the MAX document) but are not
        # offered here - they are not confirmed for the KING set command.
        slug_by_code={
            "0": "agm",
            "1": "flooded",
            "2": "user_defined",
            "3": "pylon",
            "4": "shinheung",
            "5": "weco",
            "6": "soltaro",
        },
    ),
)


@dataclass(frozen=True)
class CurrentSetting:
    """One QPIRI-backed charge-current control whose options come from the
    device's own allow-list query, never a hardcoded set of amps.

    `amp_width` is the digit count PROTOCOL.md documents for this command's
    amps field - 3 for MNCHGC (`MNCHGC0060`), 2 for MUCHGC (`MUCHGC030`).
    The two commands are not interchangeable padding-wise: an amps value
    that happens to already be N characters wide in the device's allow-list
    reply is not proof that N is the width this *set* command expects.
    """

    command: str
    list_command: str
    param_name: str
    amp_width: int


CURRENT_SETTINGS: tuple[CurrentSetting, ...] = (
    CurrentSetting(command="MNCHGC", list_command="QMCHGCR", param_name="max_charging_current", amp_width=3),
    CurrentSetting(command="MUCHGC", list_command="QMUCHGCR", param_name="max_ac_charging_current", amp_width=2),
)


def _format_current_param(amps: int, amp_width: int) -> str:
    """Single-block parallel index ("0") + amps at the command's documented
    width. `amp_width` is a floor, not a cap: P1 notes the AC field grows
    past its usual 2 digits once the value exceeds 99 A, and formatting
    with a minimum width (rather than truncating to it) produces that
    wider frame automatically instead of mangling a value that large.
    """
    return f"0{amps:0{amp_width}d}"


def _unique_id(inverter, param_name: str) -> str:
    return f"{inverter.inverter_id}_setting_{param_name}"


def _qpiri_label_map(qpiri_fields: dict, param_name: str) -> dict[str, str]:
    """code -> device-decoded label for one QPIRI sub-field, read from the
    same JSON definitions process_data uses - so a label change there
    can't silently drift out of step with what a write confirms against."""
    for field_conf in qpiri_fields.values():
        for sub in field_conf.get("complex_params", {}).get("params", ()):
            if sub.get("name") == param_name:
                return dict(sub.get("value", {}))
    return {}


def _coordinator_field(coordinator, param_name: str):
    for item in coordinator.data or ():
        if item.get("param") == param_name:
            return item.get("value")
    return None


def _new_enum_entities(hass, coordinator, inverter, qpiri_fields, settings, known: set[str]):
    """Create select entities for settings the device has actually reported
    and not yet turned into an entity. Called again on every later 'conf'
    poll, so a setting missing from a failed or refused first read still
    gets its entity once the device answers."""
    entities = []
    for setting in settings:
        if setting.param_name in known:
            continue
        if _coordinator_field(coordinator, setting.param_name) is None:
            continue
        label_by_code = _qpiri_label_map(qpiri_fields, setting.param_name)
        if not label_by_code:
            continue
        known.add(setting.param_name)
        entities.append(InverterEnumSelect(hass, coordinator, inverter, setting, label_by_code))
    return entities


def _reconcile_select_gate(hass: HomeAssistant, inverter, *, advanced: bool) -> None:
    """Apply the advanced-settings gate to every select unique_id we own."""
    for setting in ENUM_SETTINGS:
        if setting.tier2:
            set_gated_entity_disabled(hass, "select", _unique_id(inverter, setting.param_name), not advanced)
    for setting in CURRENT_SETTINGS:
        set_gated_entity_disabled(hass, "select", _unique_id(inverter, setting.param_name), not advanced)


def _watch_for_new_enum_entities(
    hass, coordinator, inverter, qpiri_fields, settings, known, async_add_entities, config_entry, *, advanced: bool
):
    @callback
    def _on_update() -> None:
        new_entities = _new_enum_entities(hass, coordinator, inverter, qpiri_fields, settings, known)
        if new_entities:
            # Clear a removed-row integration disable before HA restores it.
            _reconcile_select_gate(hass, inverter, advanced=advanced)
            async_add_entities(new_entities)

    unsub = coordinator.async_add_listener(_on_update)
    config_entry.async_on_unload(unsub)


async def _new_current_entities(hass, hub, coordinator, inverter, settings, known: set[str]):
    """Query each current's allow-list once at setup. A missing or empty
    list means the entity does not exist - never a hardcoded fallback."""
    entities = []
    for setting in settings:
        if setting.param_name in known:
            continue
        if _coordinator_field(coordinator, setting.param_name) is None:
            continue
        raw_options = await hub.async_read_amp_list(setting.list_command)
        if not raw_options:
            continue
        known.add(setting.param_name)
        entities.append(InverterCurrentSelect(hass, coordinator, inverter, setting, raw_options))
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add select entities for passed config_entry in HA."""
    hub = hass.data[DOMAIN][config_entry.entry_id]
    advanced = config_entry.options.get(CONF_ADVANCED_SETTINGS, DEFAULT_ADVANCED_SETTINGS)

    entities: list[SelectEntity] = []
    inverters = list(hub.inverters)

    for inverter in inverters:
        # Advanced settings gate: while off, tier-2 entities are never
        # created below at all. Any row left behind by a previous "on"
        # period is disabled rather than deleted, so history survives.
        # Reconcile before registration so a removed row still carrying
        # disabled_by=integration is cleared before HA restores it.
        _reconcile_select_gate(hass, inverter, advanced=advanced)

        coordinator = hub.settings_coordinator
        qpiri_fields = hub.inverter_client.GetCommandByType("conf").get("QPIRI", {})
        settings_to_offer = [s for s in ENUM_SETTINGS if not s.tier2 or advanced]

        known_enum: set[str] = set()
        entities.extend(_new_enum_entities(hass, coordinator, inverter, qpiri_fields, settings_to_offer, known_enum))
        _watch_for_new_enum_entities(
            hass,
            coordinator,
            inverter,
            qpiri_fields,
            settings_to_offer,
            known_enum,
            async_add_entities,
            config_entry,
            advanced=advanced,
        )

        if advanced:
            known_current: set[str] = set()
            entities.extend(
                await _new_current_entities(hass, hub, coordinator, inverter, CURRENT_SETTINGS, known_current)
            )

    if entities:
        async_add_entities(entities)

    # Second pass for rows that were already live (not in the deleted area).
    for inverter in inverters:
        _reconcile_select_gate(hass, inverter, advanced=advanced)


class InverterEnumSelect(CoordinatorEntity, SelectEntity):
    """Select backed by one QPIRI enum field on the shared settings coordinator.

    State always comes from the device's own answer: the write path
    re-reads QPIRI and shows whatever it actually says (raising on
    refusal or mismatch), and every later coordinator refresh that
    carries the field keeps it in step with a change made on the panel.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(self, hass, coordinator, inverter, setting: EnumSetting, label_by_code: dict[str, str]) -> None:
        super().__init__(coordinator)
        self._inverter = inverter
        self._setting = setting
        self._label_by_slug = {
            slug: label_by_code[code] for code, slug in setting.slug_by_code.items() if code in label_by_code
        }
        self._slug_by_label = {label: slug for slug, label in self._label_by_slug.items()}
        self._attr_unique_id = _unique_id(inverter, setting.param_name)
        self._attr_translation_key = setting.param_name
        self._attr_options = list(self._label_by_slug.keys())
        self._state = self._slug_from_coordinator()
        assign_entity_id(hass, self, "select", setting.param_name)

    def _slug_from_coordinator(self) -> str | None:
        label = _coordinator_field(self.coordinator, self._setting.param_name)
        if label is None:
            return None
        return self._slug_by_label.get(label)

    @property
    def device_info(self) -> DeviceInfo:
        return self._inverter.device_info

    @property
    def available(self) -> bool:
        return self._inverter.online and self._inverter.hub.online

    @property
    def current_option(self) -> str | None:
        return self._state

    @callback
    def _handle_coordinator_update(self) -> None:
        slug = self._slug_from_coordinator()
        if slug is not None:
            self._state = slug
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        code = next(c for c, s in self._setting.slug_by_code.items() if s == option)
        label = self._label_by_slug[option]
        param = f"{int(code):02d}"
        result = await self._inverter.hub.async_write_qpiri_setting(
            self._setting.command, param, self._setting.param_name, label
        )
        if not result.ok:
            raise HomeAssistantError(
                f"Inverter would not set {self._setting.param_name.replace('_', ' ')} to "
                f"{option!r} (device now reports {result.read_back!r})"
            )
        self._state = self._slug_by_label.get(result.read_back, option)
        self.schedule_update_ha_state()


class InverterCurrentSelect(CoordinatorEntity, SelectEntity):
    """Select backed by one QPIRI charge-current field, whose only valid
    options are whatever the device itself advertised via QMCHGCR/QMUCHGCR
    at setup - never a hardcoded amperage list."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(self, hass, coordinator, inverter, setting: CurrentSetting, raw_options: list[str]) -> None:
        super().__init__(coordinator)
        self._inverter = inverter
        self._setting = setting
        # Display without the leading zeros the device pads its list with,
        # but keep the exact raw string for the command param and for
        # comparing against the decoded (int) read-back.
        self._raw_by_display = {str(int(raw)): raw for raw in raw_options}
        self._attr_unique_id = _unique_id(inverter, setting.param_name)
        self._attr_translation_key = setting.param_name
        self._attr_options = list(self._raw_by_display.keys())
        self._state = self._display_from_coordinator()
        assign_entity_id(hass, self, "select", setting.param_name)

    def _display_from_coordinator(self) -> str | None:
        value = _coordinator_field(self.coordinator, self._setting.param_name)
        if value is None:
            return None
        return str(value)

    @property
    def device_info(self) -> DeviceInfo:
        return self._inverter.device_info

    @property
    def available(self) -> bool:
        return self._inverter.online and self._inverter.hub.online

    @property
    def current_option(self) -> str | None:
        return self._state

    @callback
    def _handle_coordinator_update(self) -> None:
        display = self._display_from_coordinator()
        if display is not None:
            self._state = display
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        raw = self._raw_by_display[option]
        amps = int(raw)
        param = _format_current_param(amps, self._setting.amp_width)
        result = await self._inverter.hub.async_write_qpiri_setting(
            self._setting.command, param, self._setting.param_name, amps
        )
        if not result.ok:
            raise HomeAssistantError(
                f"Inverter would not set {self._setting.param_name.replace('_', ' ')} to "
                f"{option} A (device now reports {result.read_back!r})"
            )
        self._state = str(result.read_back)
        self.schedule_update_ha_state()
