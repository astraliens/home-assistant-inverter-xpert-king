"""Registry-level enable/disable for entities gated behind an integration option.

Mirrors sensor.py's FieldAbsenceTracker: an entity this integration itself
disabled can be re-enabled by the integration once the gate opens again,
but one the user disabled by hand is left alone either way.

Home Assistant parks removed registry rows and restores them (including
disabled_by) when the same unique_id is registered again. A gate pass that
only looks up live rows therefore misses anything still in the deleted
area — clear an integration disable there too before registration so the
restored row comes back enabled when the gate is on.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Sentinel: deleted_entities could not be read. Callers must not treat this
# as "no row" — see entity_naming.assign_entity_id.
DELETED_LOOKUP_FAILED = object()


def get_deleted_entity(
    registry: er.EntityRegistry, platform_domain: str, unique_id: str
) -> Any:
    """Best-effort fetch of a removed registry row for this unique_id.

    deleted_entities is not a supported Home Assistant API. Returns the
    deleted entry, None when absent, or DELETED_LOOKUP_FAILED when the
    structure is missing, shaped differently, or the read raises.
    """
    try:
        deleted_entities = getattr(registry, "deleted_entities", None)
        if deleted_entities is None or not hasattr(deleted_entities, "get"):
            return DELETED_LOOKUP_FAILED
        return deleted_entities.get((platform_domain, DOMAIN, unique_id))
    except Exception:
        _LOGGER.debug(
            "Could not read deleted entity %s %s",
            platform_domain,
            unique_id,
            exc_info=True,
        )
        return DELETED_LOOKUP_FAILED


def set_gated_entity_disabled(
    hass: HomeAssistant, platform_domain: str, unique_id: str, disabled: bool
) -> None:
    """Disable or re-enable one entity this integration is gating, by unique_id."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform_domain, DOMAIN, unique_id)
    if entity_id is not None:
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
        return

    # No live row. When opening the gate, also clear our disable on a
    # removed row so HA's restore does not bring disabled_by=integration
    # back with the old created_at.
    if not disabled:
        _clear_integration_disable_in_deleted(registry, platform_domain, unique_id)


def _clear_integration_disable_in_deleted(
    registry: er.EntityRegistry, platform_domain: str, unique_id: str
) -> None:
    """Best-effort clear of our disable on a removed registry row.

    If the structure is missing, shaped differently, or the rebuild fails,
    leave it alone — platforms reconcile again after registration and will
    clear the disable on the restored live row (HA then reloads the entry,
    so the control appears a moment later rather than never).
    """
    try:
        import attr

        deleted = get_deleted_entity(registry, platform_domain, unique_id)
        if deleted is None or deleted is DELETED_LOOKUP_FAILED:
            return
        if getattr(deleted, "disabled_by", None) != er.RegistryEntryDisabler.INTEGRATION:
            return
        key = (platform_domain, DOMAIN, unique_id)
        registry.deleted_entities[key] = attr.evolve(deleted, disabled_by=None)
    except Exception:
        _LOGGER.debug(
            "Could not clear integration disable on deleted entity %s %s",
            platform_domain,
            unique_id,
            exc_info=True,
        )
