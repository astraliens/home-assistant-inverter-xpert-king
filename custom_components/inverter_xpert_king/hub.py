"""Hub connecting Home Assistant to the inverter client."""
from __future__ import annotations

import asyncio
import logging
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .inverter.inverterclient import InverterClient

_LOGGER = logging.getLogger(__name__)

# Accepted length band for a QID reply used as device identity. Not tied to
# our own unit's serial format - other models in this protocol family may
# use a different length or character set.
MIN_SERIAL_LEN = 4
MAX_SERIAL_LEN = 32


def normalize_serial(raw: str | None) -> str | None:
    """Return a usable device identity from a QID reply, or None.

    Rejects anything that isn't a plausible serial: empty, a NAK/ACK
    token, unprintable characters, or outside the expected length band.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or candidate in ("NAK", "ACK"):
        return None
    if not candidate.isprintable():
        return None
    if not (MIN_SERIAL_LEN <= len(candidate) <= MAX_SERIAL_LEN):
        return None
    return candidate


def generate_device_key() -> str:
    """One-time fallback identity when the serial can't be read."""
    return f"gen_{uuid.uuid4().hex}"


def _poll_by_command(inverter_client: InverterClient, command_type: str):
    """Poll every command in a group individually, off the event loop.

    Returns the group's slice of the command table (which fields each
    command is expected to answer) alongside a per-command outcome, so a
    caller can tell a genuinely parsed reply - however few fields it
    contains - from a failed one (timeout, NAK, bad frame, bad CRC). Those
    failures say nothing about whether a field is actually supported. Nor
    does a field that arrived but failed to convert - it's carried
    separately from parsed so it isn't mistaken for one that never came
    back at all.

    A NAK is carried as its own flag rather than folded into "failed": a
    device refusing a command outright is a different kind of signal from
    a timeout or a corrupt frame, even though neither counts as a
    successful poll.
    """
    command_defs = inverter_client.GetCommandByType(command_type)
    results = []
    for command in command_defs:
        raw = inverter_client.get_data(command)
        naked = raw == ["NAK"]
        ok = bool(raw) and not naked
        if ok:
            parsed = inverter_client.process_data(command, raw)
            failed_fields = set(inverter_client.last_field_failures)
        else:
            parsed, failed_fields = [], set()
        results.append((command, ok, parsed, failed_fields, naked))
    return command_defs, results


class Hub:
    """Owns the inverter socket and serializes access to it.

    Construction is cheap and does no I/O; call `async_connect` to open
    the connection. Every client call goes through `async_run`, which
    holds a single lock for the whole call so two coordinators (or a
    coordinator and a switch) never touch the socket at the same time.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        device_key: str | None = None,
        device_key_source: str | None = None,
    ) -> None:
        """Init hub."""
        self._host = host
        self._port = port
        self._hass = hass
        self._lock = asyncio.Lock()
        self.inverter_client = InverterClient(host, port)
        self._device_key_source = device_key_source
        self.inverters = [
            Inverter(device_key or host.lower(), "Inverter", self),
        ]

    @property
    def hub_id(self) -> str:
        """ID for hub."""
        return self.inverters[0].inverter_id

    @property
    def device_key_source(self) -> str | None:
        """How the current device identity was obtained ('qid' or 'generated')."""
        return self._device_key_source

    @property
    def online(self) -> bool:
        """Return True while the underlying socket is connected."""
        return self.inverter_client.connected

    async def async_run(self, func, *args):
        """Run one client call under the connection lock, off the event loop."""
        async with self._lock:
            return await self._hass.async_add_executor_job(func, *args)

    async def async_connect(self) -> None:
        """Open the connection. Safe to call even if already connected."""
        await self.async_run(self.inverter_client.connect)

    async def async_close(self) -> None:
        """Close the connection, e.g. on integration unload."""
        await self.async_run(self.inverter_client.disconnect)

    async def async_reconnect(self) -> None:
        """Reconnect under the same lock. Never raises."""
        await self.async_run(self.inverter_client.reconnect)

    async def async_poll(self, command_type: str):
        """Poll a command group, returning per-command success and parsed fields."""
        return await self.async_run(_poll_by_command, self.inverter_client, command_type)

    async def test_connection(self) -> bool:
        """Test connectivity with inverter."""
        await self.async_connect()
        if not self.online:
            return False
        return bool(await self.async_run(self.inverter_client.get_mode))


class Inverter:
    """Inverter device wrapper used by HA entities."""

    def __init__(self, inverterid: str, name: str, hub: Hub) -> None:
        """Init inverter."""
        self._id = inverterid
        self.hub = hub
        self.name = name

    @property
    def inverter_id(self) -> str:
        """Return ID for inverter."""
        return self._id

    async def reconnect(self) -> None:
        """Reconnect via the hub, used by coordinators after a failed poll."""
        await self.hub.async_reconnect()

    @property
    def online(self) -> bool:
        """Return True if the inverter connection is up."""
        return self.hub.inverter_client.connected

    @property
    def device_info(self) -> DeviceInfo:
        """HA device registry entry, shared by every entity of this inverter.

        Only fields we actually have a value for are set - a field the
        device hasn't answered yet (or never will, on this model) must
        show up as absent, not as an empty string or "Unknown".

        manufacturer is the one exception: it is set to None rather than
        left out. The device registry only overwrites a field when the
        key is present; an absent key leaves whatever was written on a
        previous version untouched. Explicitly passing None is how a
        field gets cleared, which matters here because older releases of
        this integration used to write a hardcoded brand string that no
        longer applies - the protocol has no vendor/brand command, and
        QGMN is a model-family code shared across different retail
        brands on the same hardware, not proof of who built this unit.
        """
        client = self.hub.inverter_client
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, self._id)},
            "manufacturer": None,
        }

        # QMN is the model string; QGMN falls back to it when QMN is blank.
        model = client.model or client.model_general
        info["name"] = model or self.name
        if model:
            info["model"] = model

        # QGMN's decoded text describes the hardware family (e.g. "AXPERT
        # KING"), so it belongs in hw_version, not among the firmware
        # values below.
        if client.model_general:
            info["hw_version"] = client.model_general

        sw_parts = []
        if client.cpu_firmware:
            sw_parts.append(f"CPU {client.cpu_firmware}")
        if client.panel_firmware:
            sw_parts.append(f"Panel {client.panel_firmware}")
        if client.bt_firmware:
            sw_parts.append(f"BT {client.bt_firmware}")
        if sw_parts:
            info["sw_version"] = " / ".join(sw_parts)

        if self.hub.device_key_source == "qid":
            info["serial_number"] = self._id

        return info
