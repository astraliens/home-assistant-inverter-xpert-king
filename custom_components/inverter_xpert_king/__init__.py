from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

from . import hub
from .const import CONF_DEVICE_KEY, CONF_DEVICE_KEY_SOURCE, CONF_LEGACY_DEVICE_KEY, DOMAIN

_LOGGER = logging.getLogger(__name__)

# List of platforms to support. There should be a matching .py file for each,
# eg <cover.py> and <sensor.py>
PLATFORMS: list[str] = ["sensor", "switch"]


def _compute_legacy_device_id(entry: ConfigEntry) -> str:
    """Pre-migration device identity, derived from whatever host entry.data has right now."""
    return f"{entry.data[CONF_HOST].lower()}_1"


def _get_or_freeze_legacy_device_id(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Return this entry's pre-migration identity, freezing it on first use.

    Before reconfigure existed, "the host this entry has" and "the host
    its entities were created from" were always the same value, so
    deriving this live from entry.data was harmless. Reconfigure broke
    that assumption: an entry still waiting out a migration collision
    (see async_migrate_entry) keeps using this identity for its runtime
    entities, and if the address changes underneath it, deriving fresh
    would silently swap every entity to a new identity - orphaning the
    existing ones and leaving the eventual re-key unable to find them,
    since it looks for a prefix built from the old address.

    So this is resolved at most once per entry and stored in entry.data;
    every later call - including one after the host has changed - returns
    the same value. The first call for a given entry (nothing stored yet)
    still derives it from whatever host is currently in entry.data. That
    is correct specifically because it is the *first* call: for an
    installation upgrading from before this identity existed, reconfigure
    was never available yet, so the current host is still the one every
    existing entity's unique_id was already built from - freezing it here
    reproduces that exact value instead of minting a new one.
    """
    frozen = entry.data.get(CONF_LEGACY_DEVICE_KEY)
    if frozen:
        return frozen
    frozen = _compute_legacy_device_id(entry)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LEGACY_DEVICE_KEY: frozen}
    )
    return frozen


def _migration_collision_issue_id(entry: ConfigEntry) -> str:
    return f"migration_id_collision_{entry.entry_id}"


def _heal_unique_id_mismatch(hass: HomeAssistant, entry: ConfigEntry, device_key: str) -> None:
    """Align the entry's own unique_id with device_key, without risking setup.

    This is pure upkeep: entry.data's copy of the key already drives every
    entity, on this run and on every past one, whether or not the entry's
    unique_id gets healed to match. All this accomplishes when it works is
    stopping the mismatch warning from repeating on the next start; a
    config entry that fails to load over cosmetic upkeep would be a far
    worse outcome than that warning simply recurring, so nothing here is
    allowed to raise out.

    Checked ahead of time rather than only caught after the fact: another
    entry already holding device_key as its unique_id is the one case
    where healing is genuinely impossible, not just unlucky, so it is
    worth naming that other entry in the log instead of a generic failure.
    """
    conflict = hass.config_entries.async_entry_for_domain_unique_id(entry.domain, device_key)
    if conflict is not None and conflict.entry_id != entry.entry_id:
        _LOGGER.warning(
            "%s: cannot heal its recorded identity to %s because config "
            "entry %s already uses it; this does not affect %s's own data "
            "in any way - only the mismatch warning above will keep "
            "repeating on every start until that duplicate is resolved by "
            "hand",
            entry.title, device_key, conflict.title, entry.title,
        )
        return
    try:
        hass.config_entries.async_update_entry(entry, unique_id=device_key)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.warning(
            "%s: could not heal its recorded identity to %s; this is "
            "harmless to %s's own data, but the mismatch warning above "
            "will keep repeating on every start until it is resolved",
            entry.title, device_key, entry.title,
        )


def _recover_device_key(entry: ConfigEntry) -> tuple[str, str | None]:
    """Recover a device_key missing from an already-migrated (version 2) entry.

    This should never happen in the normal lifecycle - the key is written
    to entry.data before the version is bumped past 1, and to the entry's
    own unique_id at the very same moment (see async_migrate_entry below,
    and async_step_user in config_flow.py for a fresh entry) - but
    entry.data can be edited or lost outside that lifecycle, and Hub falls
    back to a host-based identity when given none. That fallback is
    exactly the outcome this guards against: it would quietly stand up a
    second set of entities instead of failing in a way anyone would
    notice.

    The entry's unique_id was written from the exact same value at the
    exact same time as entry.data's copy, and Home Assistant keeps it
    outside entry.data - so it survives the kind of edit or storage
    hiccup that can lose entry.data's copy alone. Recovering from it needs
    no contact with the inverter, so it works whether the key came from a
    real serial or a one-time generated fallback, and unlike re-reading a
    live serial, it can never adopt a different unit's identity if the
    address now happens to point at different hardware.

    Only when both copies are gone is there nothing to recover from, and
    refusing to load is preferable to inventing an identity.
    """
    if not entry.unique_id:
        raise ConfigEntryError(
            f"{entry.title} lost its stored device identity and there is "
            "no record of it left to recover from; remove and re-add the "
            "integration"
        )
    return entry.unique_id, entry.data.get(CONF_DEVICE_KEY_SOURCE)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # A migration deferred by a duplicate id (see async_migrate_entry) leaves
    # the entry on version 1 even though a future device_key may already be
    # sitting in entry.data - using that key here would recreate the exact
    # collision the migration is waiting out. Only an entry that has actually
    # reached version 2 gets the new identity; anything older stays on the
    # frozen legacy one until the real re-key completes.
    if entry.version >= 2:
        device_key = entry.data.get(CONF_DEVICE_KEY)
        device_key_source = entry.data.get(CONF_DEVICE_KEY_SOURCE)
        if device_key and entry.unique_id and device_key != entry.unique_id:
            # Both copies exist but disagree - written together, so this
            # means one of them was changed on its own since. entry.data's
            # copy is the one actually driving entity creation below (and
            # has been on every past run too), so switching to unique_id
            # here would silently orphan every entity already in the
            # registry - the exact outcome this whole recovery path exists
            # to prevent. Keep entry.data's value, log the disagreement so
            # it doesn't go unnoticed, and heal the entry's unique_id to
            # match so this check stops firing on the next start.
            _LOGGER.warning(
                "%s: stored device key (%s) disagrees with the entry's own "
                "recorded identity (%s); keeping %s because its existing "
                "entities are already built from it",
                entry.title, device_key, entry.unique_id, device_key,
            )
            _heal_unique_id_mismatch(hass, entry, device_key)
        elif not device_key:
            device_key, device_key_source = _recover_device_key(entry)
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DEVICE_KEY: device_key}
            )
    else:
        device_key = _get_or_freeze_legacy_device_id(hass, entry)
        device_key_source = None

    # Store an instance of the "connecting" class that does the work of speaking
    # with your actual devices.
    inverter_hub = hub.Hub(
        hass,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        device_key,
        device_key_source,
    )
    await inverter_hub.async_connect()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = inverter_hub

    # This creates each HA object for each platform your device requires.
    # It's done by calling the `async_setup_entry` function in each platform module.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # This is called when an entry/configured device is to be removed. The class
    # needs to unload itself, and remove callbacks. See the classes for further
    # details
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        inverter_hub = hass.data[DOMAIN].pop(entry.entry_id)
        await inverter_hub.async_close()

    return unload_ok


def _find_entity_collision(entity_registry, entry_id, old_prefix, device_key):
    """Return (old_entity_id, claimed_by_entity_id) for the first entity of
    this entry whose migrated unique id is already claimed by some other
    entity, or None if the whole rename is safe to perform.

    Entities of this same entry that were already renamed by an earlier,
    interrupted run no longer start with old_prefix, so they never appear
    here as "the one being migrated" - only as a possible claimant, which
    is exactly the case this function must not mistake for a collision if
    it's the very same row (checked via entity_id equality below).
    """
    for entity_entry in er.async_entries_for_config_entry(entity_registry, entry_id):
        if not entity_entry.unique_id.startswith(old_prefix):
            continue
        suffix = entity_entry.unique_id[len(old_prefix):]
        new_uid = f"{device_key}_{suffix}"
        claimed_by = entity_registry.async_get_entity_id(entity_entry.domain, DOMAIN, new_uid)
        if claimed_by is not None and claimed_by != entity_entry.entity_id:
            return entity_entry.entity_id, claimed_by
    return None


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move identity from the gateway address to the inverter serial (v1 -> v2).

    Only re-keys the entity/device unique ids and bumps the entry version;
    entity_id is never touched (recorder history and automations key on
    that, not unique_id), and no entity is disabled here - there is no
    poll evidence yet at migration time to tell a genuinely unsupported
    field from one that simply hasn't answered on this run.

    A duplicate id blocks only the re-key, never the entry itself: this
    returns True without bumping the version, so setup goes ahead on the
    previous identifiers (see the version gate in async_setup_entry above)
    instead of leaving the whole device dark. Because the version is left
    at 1, Home Assistant calls this function again on every subsequent
    start, so removing the duplicate finishes the migration on its own
    without further action once the user clears it. A repair issue tells
    them what's blocking it in the meantime.
    """
    issue_id = _migration_collision_issue_id(entry)

    if entry.version == 2:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return True
    if entry.version > 2:
        # A newer release migrated this entry further than this build
        # understands; refuse rather than silently running against data
        # it can't interpret.
        return False

    old_inverter_id = _get_or_freeze_legacy_device_id(hass, entry)
    old_prefix = f"{old_inverter_id}_"

    device_key, source = await _async_resolve_device_key(hass, entry)
    if device_key is None:
        _LOGGER.warning(
            "Cannot migrate %s: inverter unreachable and no device key stored yet; "
            "will retry on next start",
            entry.title,
        )
        return False

    # Record the resolved key before anything in the registry is touched.
    # The version stays at 1, so the entry is still treated as unfinished,
    # but a run interrupted from here on resumes with this same key
    # instead of _async_resolve_device_key generating a second, different
    # fallback next time - which would leave some entities on the first
    # generated key and others on the second, with no way back.
    if entry.data.get(CONF_DEVICE_KEY) != device_key:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_DEVICE_KEY: device_key, CONF_DEVICE_KEY_SOURCE: source},
        )

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    # Compute every target id and check for collisions before rewriting
    # anything. The registry rejects a duplicate unique_id, and finding
    # that out mid-rewrite would leave some entities re-keyed and others
    # not - a retry then can't even see the already-migrated ones, since
    # they no longer match old_prefix, and fails on the same collision
    # forever. A genuine collision defers the re-key rather than being
    # guessed at, because the guess could silently drop an entity - but it
    # does not take the entry down: the device keeps reporting on its
    # current ids while the user clears the duplicate by hand.
    collision = _find_entity_collision(entity_registry, entry.entry_id, old_prefix, device_key)
    if collision is not None:
        old_entity_id, claimed_by = collision
        _LOGGER.warning(
            "Identity migration for %s deferred: %s and %s would both claim the "
            "unique id derived from %s; the integration keeps running on its "
            "current identifiers until the duplicate is resolved by hand",
            entry.title, old_entity_id, claimed_by, device_key,
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="migration_id_collision",
            translation_placeholders={
                "title": entry.title,
                "reason": f"{old_entity_id} and {claimed_by} would end up as the same entity",
            },
        )
        return True

    old_device = device_registry.async_get_device(identifiers={(DOMAIN, old_inverter_id)})
    if old_device is not None:
        conflict = device_registry.async_get_device(identifiers={(DOMAIN, device_key)})
        if conflict is not None and conflict.id != old_device.id:
            _LOGGER.warning(
                "Identity migration for %s deferred: device identifier %s is "
                "already in use by another device; the integration keeps running "
                "on its current identifiers until the duplicate is resolved by hand",
                entry.title, device_key,
            )
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="migration_id_collision",
                translation_placeholders={
                    "title": entry.title,
                    "reason": (
                        "this inverter's device would end up merged with a "
                        "different device that is already set up"
                    ),
                },
            )
            return True

    # No collision on this attempt - drop any issue a previous attempt
    # raised, it no longer applies.
    ir.async_delete_issue(hass, DOMAIN, issue_id)

    def _migrate_unique_id(entity_entry):
        if not entity_entry.unique_id.startswith(old_prefix):
            return None
        suffix = entity_entry.unique_id[len(old_prefix):]
        return {"new_unique_id": f"{device_key}_{suffix}"}

    migrated = 0

    def _count_and_migrate(entity_entry):
        nonlocal migrated
        updates = _migrate_unique_id(entity_entry)
        if updates is not None:
            migrated += 1
        return updates

    await er.async_migrate_entries(hass, entry.entry_id, _count_and_migrate)

    device_updated = False
    if old_device is not None:
        device_registry.async_update_device(old_device.id, new_identifiers={(DOMAIN, device_key)})
        device_updated = True

    hass.config_entries.async_update_entry(
        entry,
        unique_id=device_key,
        data={**entry.data, CONF_DEVICE_KEY: device_key, CONF_DEVICE_KEY_SOURCE: source},
        version=2,
    )
    _LOGGER.info(
        "Migrated %s from %s to serial-based identity %s (%d entities re-keyed, device updated: %s)",
        entry.title, old_inverter_id, device_key, migrated, device_updated,
    )
    return True


async def _async_resolve_device_key(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[str | None, str | None]:
    """Return (device_key, source) to migrate this entry to.

    Prefers an already-stored key (a retry of a previously interrupted
    migration), then a freshly read serial, then a one-time generated
    fallback - but only once we've actually reached the device. An
    unreachable device with no stored key yields (None, None) so the
    caller can fail closed and retry later, rather than permanently
    generating an identity for a device we've never talked to.
    """
    stored = entry.data.get(CONF_DEVICE_KEY)
    if stored:
        return stored, entry.data.get(CONF_DEVICE_KEY_SOURCE, "generated")

    probe = hub.Hub(hass, entry.data[CONF_HOST], entry.data[CONF_PORT])
    try:
        await probe.async_connect()
        if not probe.online:
            return None, None
        serial = await probe.async_run(probe.inverter_client.get_serial)
        device_key = hub.normalize_serial(serial)
        if device_key:
            return device_key, "qid"
        return hub.generate_device_key(), "generated"
    finally:
        await probe.async_close()
