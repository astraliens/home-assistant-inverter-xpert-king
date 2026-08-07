"""Stable entity_id suggestions for newly created entities.

Home Assistant keeps a stored entity_id once a unique_id has a registry
row, so these helpers only shape first-time registration. Existing
installs keep whatever slug they already have.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .entity_gate import DELETED_LOOKUP_FAILED, get_deleted_entity

# Diagnostic connection sensor — not a wire field; absence tracking and
# the advanced-settings gate must never look this key up as one.
CONNECTION_ENTITY_KEY = "connection"


def slug_part(value: str) -> str:
    """Slugify one segment of an object id."""
    return slugify(value, separator="_")


def connection_target(hub) -> str:
    """Endpoint shown as the connection sensor state.

    TCP today is host:port. When serial transport lands, extend this one
    helper so both the diagnostic state and the identity fallback stay
    aligned.
    """
    return f"{hub.host}:{hub.port}"


def connection_identity_slug(hub) -> str:
    """Identity segment when the device key did not come from the device.

    TCP: host and port slugified together. Serial later: put the port
    path in this same slot rather than inventing a second scheme.
    """
    return slug_part(f"{hub.host}_{hub.port}")


def identity_slug(inverter) -> str:
    """Serial when QID supplied the device key; otherwise the connection."""
    hub = inverter.hub
    if hub.device_key_source == "qid":
        return slug_part(inverter.inverter_id)
    return connection_identity_slug(hub)


def model_slug(hub) -> str | None:
    """Slug of the model from QMN/QGMN, or None when the device has not reported one."""
    client = hub.inverter_client
    model = client.model or client.model_general
    if not model:
        return None
    return slug_part(model)


def object_id_for(inverter, entity_key: str) -> str:
    """model_identity_entitykey — model omitted when the device reported none."""
    parts: list[str] = []
    model = model_slug(inverter.hub)
    if model:
        parts.append(model)
    parts.append(identity_slug(inverter))
    parts.append(entity_key)
    return "_".join(parts)


def suggested_entity_id(platform: str, inverter, entity_key: str) -> str:
    """Full entity_id suggestion for a never-before-seen unique_id."""
    return f"{platform}.{object_id_for(inverter, entity_key)}"


def assign_entity_id(
    hass: HomeAssistant, entity, platform: str, entity_key: str
) -> None:
    """Set entity_id before Home Assistant registers the entity.

    If a removed registry row still exists for this unique_id, reuse its
    entity_id. Newer Home Assistant restores that automatically; older
    ones do not, and without the reuse a delete-and-re-add would pick up
    the new naming scheme. If that lookup fails, leave entity_id unset so
    HA falls back to its usual name derivation. Only a unique_id the
    registry has never seen gets the identity-based suggestion.
    """
    unique_id = getattr(entity, "_attr_unique_id", None) or getattr(
        entity, "unique_id", None
    )
    deleted = get_deleted_entity(er.async_get(hass), platform, unique_id)
    if deleted is DELETED_LOOKUP_FAILED:
        return
    if deleted is not None:
        old_id = getattr(deleted, "entity_id", None)
        if isinstance(old_id, str) and old_id:
            entity.entity_id = old_id
        return

    entity.entity_id = suggested_entity_id(platform, entity._inverter, entity_key)
