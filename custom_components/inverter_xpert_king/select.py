"""Platform for select integration: enum-shaped QPIRI settings."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir, translation
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .battery_apply import VOLTAGE_ORDER_CONFLICT_KEY, ApplyOutcome, ApplyResult, FieldRef
from .battery_current import CURRENT_SETTINGS, CurrentSetting, _format_current_param
from .battery_profile_storage import profile_container, usable_profiles
from .const import (
    CONF_ADVANCED_SETTINGS,
    DEFAULT_ADVANCED_SETTINGS,
    DOMAIN,
    ISSUE_PROFILE_APPLY_MIXED_STATE,
    ISSUE_PROFILE_APPLY_TRANSPORT_UNKNOWN,
    PROFILE_FIELD_ORDER,
    PROFILE_FIELD_TO_QPIRI_PARAM,
)
from .entity_gate import set_gated_entity_disabled
from .entity_naming import assign_entity_id
from .number import _describe_unconfirmed_write

_LOGGER = logging.getLogger(__name__)


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

        profiles = usable_profiles(profile_container(config_entry.options))
        _reconcile_profile_select_gate(hass, inverter, enabled=advanced and bool(profiles))
        if advanced and profiles:
            entities.append(InverterProfileSelect(hass, coordinator, inverter, hub, profiles))

    if entities:
        async_add_entities(entities)

    # Second pass for rows that were already live (not in the deleted area).
    for inverter in inverters:
        _reconcile_select_gate(hass, inverter, advanced=advanced)
        _reconcile_profile_select_gate(
            hass, inverter, enabled=advanced and bool(usable_profiles(profile_container(config_entry.options)))
        )


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
            setting_label = self._setting.param_name.replace('_', ' ')
            if result.read_back is None:
                raise HomeAssistantError(_describe_unconfirmed_write(setting_label, result))
            raise HomeAssistantError(
                f"Inverter would not set {setting_label} to "
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
            setting_label = self._setting.param_name.replace('_', ' ')
            if result.read_back is None:
                raise HomeAssistantError(_describe_unconfirmed_write(setting_label, result))
            raise HomeAssistantError(
                f"Inverter would not set {setting_label} to "
                f"{option} A (device now reports {result.read_back!r})"
            )
        self._state = str(result.read_back)
        self.schedule_update_ha_state()


# --- Battery profile select --------------------------------------------

_PROFILE_SELECT_PARAM = "battery_profile"


def _profile_select_unique_id(inverter) -> str:
    return _unique_id(inverter, _PROFILE_SELECT_PARAM)


def _reconcile_profile_select_gate(hass: HomeAssistant, inverter, *, enabled: bool) -> None:
    set_gated_entity_disabled(hass, "select", _profile_select_unique_id(inverter), not enabled)


def _live_param_values(coordinator) -> dict[str, object]:
    return {item.get("param"): item.get("value") for item in (coordinator.data or ())}


def profile_matches_live(profile: dict, live: dict[str, object]) -> bool:
    """True when every explicit (non-DNC) field of `profile` equals the
    corresponding live value. DNC fields never participate."""
    for profile_field in PROFILE_FIELD_ORDER:
        target = profile.get(profile_field)
        if target is None:
            continue
        if live.get(PROFILE_FIELD_TO_QPIRI_PARAM[profile_field]) != target:
            return False
    return True


def matching_profile_name(profiles: list[dict], live: dict[str, object]) -> str | None:
    """Exact-match profile name, or None for no match or more than one.

    More than one match is a normal, savable configuration: only true
    duplicates are refused at save time, so profiles whose explicit
    fields do not conflict - one setting currents, another voltages -
    can both match the same live state. Never guess by picking the
    first; unknown is the only honest state to show. That state is
    otherwise indistinguishable from a plain no-match, so which
    profiles overlapped is logged rather than silently swallowed.
    """
    matches = [p["name"] for p in profiles if profile_matches_live(p, live)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _LOGGER.warning(
            "Battery profiles %s all match the inverter's current settings at "
            "once; not reporting any of them as selected",
            matches,
        )
    return None


def _apply_issue_id(inverter) -> str:
    # One issue id per inverter regardless of which of the two variants
    # below is currently showing - re-raising with a different
    # translation_key replaces the previous wording under the same id.
    return f"{ISSUE_PROFILE_APPLY_MIXED_STATE}_{inverter.inverter_id}"


def _raise_apply_mixed_state_issue(
    hass: HomeAssistant, inverter, message: str, *, translation_key: str = ISSUE_PROFILE_APPLY_MIXED_STATE
) -> None:
    """Amendment 5a: an unconfirmed rollback and a connection lost with no
    rollback attempted are not the same situation, so they use different
    translation keys - never present a lost connection as an unconfirmed
    undo, or the other way round."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _apply_issue_id(inverter),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=translation_key,
        translation_placeholders={
            "title": getattr(inverter, "name", None) or inverter.inverter_id,
            "detail": message,
        },
    )


def _clear_apply_mixed_state_issue(hass: HomeAssistant, inverter) -> None:
    ir.async_delete_issue(hass, DOMAIN, _apply_issue_id(inverter))


# --- Localizing battery_apply's translation placeholders --------------------
#
# battery_apply.py has no `hass` and must stay that way (see its module
# docstring), so a placeholder value wrapped in `battery_apply.FieldRef`
# carries a raw PROFILE_FIELD_* identifier, not yet a word a user would
# recognize. This is the one place that resolves it - through the
# add-profile form's own already-translated labels (the exact words on
# the profile form), never a second naming scheme - before the result
# reaches HomeAssistantError. The `FieldRef` type, not a naming convention
# on the placeholder's key, is what marks a value as needing this: an
# eleventh message that forgets to wrap its field id this way leaves it
# unresolved rather than silently passing as already a label.

# A custom top-level category, not one of HA's well-known ones (config,
# options, entity, exceptions...) - but the loader treats none of those
# as a fixed whitelist. It reads the whole translations/<lang>.json file
# for the domain and turns whatever top-level keys it finds into
# categories (homeassistant.helpers.translation.build_resources and
# _TranslationCache._build_category_cache, calling
# homeassistant.loader.Integration.has_translations - checked against
# Home Assistant 2026.7.4, the version this repo's dev/CT environment
# runs). test_apply_fragments_category_resolves_through_real_ha_translation_uk
# in test_apply_localization.py pins that against the real loader, not a
# mock, so a future HA change that stops honoring a custom category
# fails a test instead of silently shipping mixed-language text.
_APPLY_FRAGMENTS_CATEGORY = "apply_fragments"


def _is_field_placeholder(value) -> bool:
    return isinstance(value, FieldRef)


async def _resolve_profile_field_label(hass: HomeAssistant, field_name: str) -> str:
    """The add-profile form's own label for one profile field, or the plain
    English name if that lookup fails - never the raw key, never a raise."""
    translated = await translation.async_get_translations(
        hass, hass.config.language, "options", integrations={DOMAIN}
    )
    key = f"component.{DOMAIN}.options.step.add_profile.data.{field_name}"
    return translated.get(key) or field_name.replace('_', ' ')


async def _compose_voltage_order_blockers(hass: HomeAssistant, placeholders: dict[str, str]) -> str:
    """Join the one or two blocking-neighbour clauses a voltage-order
    conflict carries, in the wording `battery_apply._describe_voltage_conflict`
    settled on - `lower_blocker_field`/`upper_blocker_field` are already
    resolved to their localized labels by the time this runs."""
    fragments = await translation.async_get_translations(
        hass, hass.config.language, _APPLY_FRAGMENTS_CATEGORY, integrations={DOMAIN}
    )

    def fragment(key: str, default: str) -> str:
        full_key = f"component.{DOMAIN}.{_APPLY_FRAGMENTS_CATEGORY}.{key}"
        if full_key not in fragments:
            # Should not happen with our own translations shipped and the
            # category confirmed to resolve (see the comment above
            # _APPLY_FRAGMENTS_CATEGORY) - if it ever does, the English
            # default below is a real language mix for a non-English
            # user, not a harmless fallback, so it must show up in the log.
            _LOGGER.warning(
                "Apply fragment %r missing from %r translations; using the English default",
                key, hass.config.language,
            )
            return default
        return fragments[full_key]

    clauses = []
    for prefix in ("lower", "upper"):
        field_key = f"{prefix}_blocker_field"
        if field_key not in placeholders:
            continue
        unchanged = placeholders.get(f"{prefix}_blocker_unchanged") == "1"
        template = fragment(
            "voltage_order_blocker_unchanged" if unchanged else "voltage_order_blocker",
            "{name} is currently {value} (this profile leaves it unchanged)" if unchanged
            else "{name} is currently {value}",
        )
        clauses.append(template.format(name=placeholders[field_key], value=placeholders[f"{prefix}_blocker_value"]))
    join = fragment("voltage_order_join", " and ")
    return join.join(clauses)


async def _localize_apply_placeholders(hass: HomeAssistant, result: ApplyResult) -> dict[str, str]:
    placeholders = dict(result.translation_placeholders)
    for key, value in list(placeholders.items()):
        if _is_field_placeholder(value):
            placeholders[key] = await _resolve_profile_field_label(hass, value)
    if result.translation_key == VOLTAGE_ORDER_CONFLICT_KEY:
        placeholders["blockers"] = await _compose_voltage_order_blockers(hass, placeholders)
        for extra in (
            "lower_blocker_field", "lower_blocker_value", "lower_blocker_unchanged",
            "upper_blocker_field", "upper_blocker_value", "upper_blocker_unchanged",
        ):
            placeholders.pop(extra, None)
    return placeholders


class InverterProfileSelect(CoordinatorEntity, SelectEntity):
    """Applies a whole battery profile as one explicit transaction.

    Options are exact profile names - the same string an automation
    passes to `select.select_option`. State reflects only a confirmed
    exact live match; it is never set to the option just clicked before
    the device has actually confirmed it (see `async_select_option`).
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_translation_key = _PROFILE_SELECT_PARAM

    def __init__(self, hass, coordinator, inverter, hub, profiles: list[dict]) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._inverter = inverter
        self._hub = hub
        self._profiles_by_name = {p["name"]: p for p in profiles}
        self._attr_unique_id = _profile_select_unique_id(inverter)
        self._attr_options = list(self._profiles_by_name)
        self._state = self._match_from_coordinator()
        assign_entity_id(hass, self, "select", _PROFILE_SELECT_PARAM)

    def _match_from_coordinator(self) -> str | None:
        live = _live_param_values(self.coordinator)
        return matching_profile_name(list(self._profiles_by_name.values()), live)

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
        self._state = self._match_from_coordinator()
        if self._state is not None:
            _clear_apply_mixed_state_issue(self._hass, self._inverter)
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        profile = self._profiles_by_name.get(option)
        if profile is None:
            raise HomeAssistantError(f"{option!r} is not a known battery profile")

        result = await self._hub.async_apply_battery_profile(profile)

        if result.outcome is ApplyOutcome.ROLLBACK_INCOMPLETE:
            _raise_apply_mixed_state_issue(
                self._hass, self._inverter, result.message,
                translation_key=ISSUE_PROFILE_APPLY_MIXED_STATE,
            )
        elif result.outcome is ApplyOutcome.TRANSPORT_UNKNOWN and any(
            step.transmitted for step in result.sent_steps
        ):
            # Amendment 5a: the criterion is whether a set frame is
            # reliably known to have actually left the socket for at
            # least one step, not just whether an attempt was journaled.
            # A transport failure before anything was sent at all - no
            # attempts yet (see the preflight-read failure case, which
            # reaches this method as PREFLIGHT_FAILED instead), or this
            # step's own send failing before transmitting anything -
            # changed nothing, so it raises no issue.
            _raise_apply_mixed_state_issue(
                self._hass, self._inverter, result.message,
                translation_key=ISSUE_PROFILE_APPLY_TRANSPORT_UNKNOWN,
            )
        elif result.outcome is ApplyOutcome.SUCCESS:
            _clear_apply_mixed_state_issue(self._hass, self._inverter)

        self._state = self._match_from_coordinator()
        self.schedule_update_ha_state()

        if result.outcome is not ApplyOutcome.SUCCESS:
            if result.translation_key is not None:
                placeholders = await _localize_apply_placeholders(self._hass, result)
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=result.translation_key,
                    translation_placeholders={"profile": option, **placeholders},
                )
            raise HomeAssistantError(f"Applying profile {option!r} failed: {result.message}")
