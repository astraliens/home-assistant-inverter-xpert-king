"""Hub connecting Home Assistant to the inverter client."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, TIME_SYNC_TOLERANCE_SECONDS
from .inverter.inverterclient import InverterClient
from .inverter.qflag import WRITABLE_LETTERS, decode_flag_status, letter_to_key

_LOGGER = logging.getLogger(__name__)

_TIME_SYNC_TOLERANCE = timedelta(seconds=TIME_SYNC_TOLERANCE_SECONDS)

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


def poll_got_any_reply(results) -> bool:
    """True when the device answered at least one command in this poll.

    A successfully parsed reply and a NAK both count - the device spoke.
    An empty outcome (timeout, closed link, bad frame) does not.
    """
    return any(ok or naked for _, ok, _, _, naked in results)


@dataclass
class FlagWriteResult:
    """Outcome of one PE/PD write, confirmed against a fresh QFLAG read.

    `ok` is the only field callers should act on for success/failure -
    it is True only when the device accepted the frame (or the letter
    was already at the requested value) *and* QFLAG agrees afterwards.
    The rest exists for building an error message.
    """

    ok: bool
    accepted: bool
    read_back: bool | None
    sent: bool


def _write_flag(inverter_client: InverterClient, letter: str, enabled: bool) -> FlagWriteResult:
    """Set one QFLAG letter and confirm it from a fresh QFLAG read.

    Rejects anything outside `WRITABLE_LETTERS` before touching the
    socket at all - this is the one place every PE/PD write goes
    through, so it is also the one place that has to refuse a letter we
    deliberately never expose, no matter who calls it or why.

    Reads QFLAG first rather than trusting whatever the entity last
    cached: that's what lets a value already changed from the front
    panel be detected instead of relying on stale in-memory state, and
    it's what makes the "don't resend an unchanged value" rule accurate
    instead of a guess. When the letter's current state already matches,
    PE/PD is never sent at all.
    """
    if letter not in WRITABLE_LETTERS:
        raise ValueError(f"{letter!r} is not an approved writable QFLAG letter")

    key = letter_to_key(letter)

    raw = inverter_client.get_data('QFLAG')
    if not raw or raw == ['NAK']:
        return FlagWriteResult(ok=False, accepted=False, read_back=None, sent=False)

    current = decode_flag_status(raw[0]).flags.get(key) if key else None
    if current == enabled:
        return FlagWriteResult(ok=True, accepted=True, read_back=current, sent=False)

    command = 'PE' if enabled else 'PD'
    reply = inverter_client.get_data(command, letter)
    accepted = reply == ['ACK']

    raw_after = inverter_client.get_data('QFLAG')
    if not raw_after or raw_after == ['NAK']:
        return FlagWriteResult(ok=False, accepted=accepted, read_back=None, sent=True)

    read_back = decode_flag_status(raw_after[0]).flags.get(key) if key else None
    ok = accepted and read_back == enabled
    return FlagWriteResult(ok=ok, accepted=accepted, read_back=read_back, sent=True)


@dataclass
class TimeSyncResult:
    """Outcome of one time-sync attempt, confirmed against a fresh QT read.

    `sent` tells apart "already in sync, nothing was written" from
    "DAT was actually sent" - both count as `ok` when the clock ends up
    within tolerance. `device_time`/`target_time` are only for building
    an error message - `ok` already accounts for the allowed tolerance
    between them.
    """

    ok: bool
    accepted: bool
    device_time: datetime | None
    target_time: datetime
    sent: bool


def _read_device_time(inverter_client: InverterClient) -> datetime | None:
    """QT reports a four-digit year where DAT takes a two-digit one - a
    documented width mismatch, not a bug - so this always returns a real
    datetime rather than a string, and any failure to get one is None."""
    raw = inverter_client.get_data('QT')
    if not raw or raw == ['NAK']:
        return None
    try:
        return datetime.strptime(raw[0], '%Y%m%d%H%M%S')
    except ValueError:
        return None


def _sync_time(inverter_client: InverterClient) -> TimeSyncResult:
    """Sync the device clock to local time, only writing when it has drifted.

    Reads QT first: if the device is already within tolerance, DAT is
    never sent at all - the same redundant-write suppression the flag
    writes use. Otherwise DAT is sent and QT is read again, and only a
    read-back within tolerance counts as success. A few seconds of
    difference either way is expected (the round trip between requests,
    plus whatever the device's own clock does in that gap) and is not
    treated as drift.
    """
    target = datetime.now().replace(microsecond=0)

    current_time = _read_device_time(inverter_client)
    if current_time is not None and abs(current_time - target) <= _TIME_SYNC_TOLERANCE:
        return TimeSyncResult(ok=True, accepted=True, device_time=current_time, target_time=target, sent=False)

    reply = inverter_client.get_data('DAT', target.strftime('%y%m%d%H%M%S'))
    accepted = reply == ['ACK']

    device_time = _read_device_time(inverter_client)
    ok = accepted and device_time is not None and abs(device_time - target) <= _TIME_SYNC_TOLERANCE
    return TimeSyncResult(ok=ok, accepted=accepted, device_time=device_time, target_time=target, sent=True)


@dataclass
class SettingWriteResult:
    """Outcome of one QPIRI-backed setting write, confirmed against a fresh
    QPIRI read.

    `read_back` is whatever the device now actually reports for that
    field, decoded the same way the sensor platform decodes it (label
    text for an enum, a float for a voltage, an int for a current) - never
    the value that was asked for. `ok` is the only field callers should
    act on for success/failure; the rest exists for building an error
    message, the same shape as `FlagWriteResult`.
    """

    ok: bool
    accepted: bool
    read_back: object
    sent: bool


def _read_qpiri_fields(inverter_client: InverterClient) -> dict[str, object] | None:
    """Decoded QPIRI fields keyed by name, or None if the read failed or
    was refused. Shares the same decoding sensor.py's own QPIRI poll uses,
    so a value compared here always means the same thing it would mean on
    a sensor - no separate raw-index table to keep in step with the JSON."""
    raw = inverter_client.get_data('QPIRI')
    if not raw or raw == ['NAK']:
        return None
    parsed = inverter_client.process_data('QPIRI', raw)
    return {item['param']: item['value'] for item in parsed}


def _write_qpiri_setting(
    inverter_client: InverterClient,
    command: str,
    param: str,
    field_name: str,
    target_value: object,
) -> SettingWriteResult:
    """Set one QPIRI-backed field and confirm it from a fresh QPIRI read.

    Reads QPIRI first, exactly like `_write_flag` reads QFLAG first: lets a
    value already changed from the front panel be detected instead of
    trusting stale in-memory state, and makes the "don't resend an
    unchanged value" rule accurate. When the field already matches, the
    set command is never sent at all.
    """
    before = _read_qpiri_fields(inverter_client)
    if before is None or field_name not in before:
        return SettingWriteResult(ok=False, accepted=False, read_back=None, sent=False)

    if before[field_name] == target_value:
        return SettingWriteResult(ok=True, accepted=True, read_back=before[field_name], sent=False)

    reply = inverter_client.get_data(command, param)
    accepted = reply == ['ACK']

    after = _read_qpiri_fields(inverter_client)
    if after is None or field_name not in after:
        return SettingWriteResult(ok=False, accepted=accepted, read_back=None, sent=True)

    read_back = after[field_name]
    ok = accepted and read_back == target_value
    return SettingWriteResult(ok=ok, accepted=accepted, read_back=read_back, sent=True)


def _read_amp_list(inverter_client: InverterClient, command: str) -> list[str]:
    """Raw space-separated allow-list from QMCHGCR/QMUCHGCR, or [] if the
    device didn't answer with anything usable. Never a hardcoded list -
    an empty result here means the corresponding select must not exist."""
    raw = inverter_client.get_data(command)
    if not raw or raw == ['NAK']:
        return []
    return [item for item in raw if item]


class InverterSettingsCoordinator(DataUpdateCoordinator):
    """Polls the 'conf' command group (QPIRI, plus identity commands) on a
    shared 600-second interval.

    Created once in __init__.py, before any platform is set up, and handed
    out from there as hub.settings_coordinator - the sensor, select and
    number platforms all read from this same coordinator instead of
    polling QPIRI again on their own.
    """

    def __init__(self, hass, hub, inverter):
        super().__init__(
            hass,
            _LOGGER,
            name="Inverter Settings Coordinator",
            update_interval=timedelta(seconds=600),
        )
        self.hub = hub
        self.inverter = inverter
        self.absence_tracker = None
        # The most recent successful poll's raw (command_defs, results), so
        # a consumer that attaches after the first refresh already ran
        # (sensor.py's own FieldAbsenceTracker) can still observe it.
        self.last_command_defs: dict = {}
        self.last_results: list = []

    async def _async_update_data(self):
        try:
            async with asyncio.timeout(30):
                command_defs, results = await self.hub.async_poll('conf')
        except Exception as exc:
            _LOGGER.error("Inverter settings update failed: %s", exc)
            await self.inverter.reconnect()
            raise UpdateFailed(str(exc)) from exc

        self.last_command_defs, self.last_results = command_defs, results
        if self.absence_tracker is not None:
            self.absence_tracker.observe(command_defs, results)

        return [item for _, _ok, parsed, *_ in results for item in parsed]


class InverterDataCoordinator(DataUpdateCoordinator):
    """Polls the 'data' command group (QPIGS, QMOD, QPIWS, QFLAG, QET, QLT,
    QLD, QPIGS2) on a shared 7-second interval.

    Created once in __init__.py, before any platform is set up, and handed
    out from there as hub.data_coordinator - every platform that needs one
    of these fields reads from this same coordinator instead of polling
    again. QFLAG is why that matters here specifically: the toggle
    switches discover themselves from the exact same reply the flag_status
    sensor already gets, rather than running a second independent poller
    of the same command over the 2400-baud link.
    """

    def __init__(self, hass, hub, inverter, *, require_reply_at_setup: bool = False):
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Inverter Data Coordinator",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=7),
        )
        self.hub = hub
        self.inverter = inverter
        self.absence_tracker = None
        # The most recent successful poll's raw (command_defs, results), so
        # a consumer that attaches after the first refresh already ran
        # (e.g. sensor.py's own FieldAbsenceTracker) can still observe it.
        self.last_command_defs: dict = {}
        self.last_results: list = []
        # Setup asks for at least one real reply before the entry is
        # considered ready. Cleared after that first useful poll so a
        # later empty cycle stays a soft failure (reconnect next time)
        # instead of taking the integration down.
        self._require_reply_at_setup = require_reply_at_setup

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            async with asyncio.timeout(30):
                command_defs, results = await self.hub.async_poll('data')

        except Exception as exc:
            _LOGGER.error("Inverter data update failed: %s", exc)
            await self.inverter.reconnect()
            raise UpdateFailed(str(exc)) from exc

        self.last_command_defs, self.last_results = command_defs, results
        if self.absence_tracker is not None:
            self.absence_tracker.observe(command_defs, results)

        if self._require_reply_at_setup and not poll_got_any_reply(results):
            raise UpdateFailed("Inverter did not answer any poll command")

        self._require_reply_at_setup = False
        return [item for _, _ok, parsed, *_ in results for item in parsed]


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
        # Set by __init__.py right after construction, before any platform
        # is set up - see InverterDataCoordinator's docstring for why it is
        # created once here rather than per-platform.
        self.data_coordinator: InverterDataCoordinator | None = None
        # Same idea, for the 'conf' group.
        self.settings_coordinator: InverterSettingsCoordinator | None = None

    @property
    def hub_id(self) -> str:
        """ID for hub."""
        return self.inverters[0].inverter_id

    @property
    def host(self) -> str:
        """Configured TCP host (serial transport will add its own endpoint)."""
        return self._host

    @property
    def port(self) -> int:
        """Configured TCP port."""
        return self._port

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
        """Open the connection. No-op when the socket is already live."""
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

    async def async_write_flag(self, letter: str, enabled: bool) -> FlagWriteResult:
        """Set one QFLAG letter and confirm it, under the same lock as polling."""
        return await self.async_run(_write_flag, self.inverter_client, letter, enabled)

    async def async_sync_time(self) -> TimeSyncResult:
        """Sync the device clock to local time; sends DAT only if it drifted."""
        return await self.async_run(_sync_time, self.inverter_client)

    async def async_write_qpiri_setting(
        self, command: str, param: str, field_name: str, target_value: object
    ) -> SettingWriteResult:
        """Set one QPIRI-backed field and confirm it, under the same lock as polling."""
        return await self.async_run(
            _write_qpiri_setting, self.inverter_client, command, param, field_name, target_value
        )

    async def async_read_amp_list(self, command: str) -> list[str]:
        """Read a QMCHGCR/QMUCHGCR allow-list, under the same lock as polling."""
        return await self.async_run(_read_amp_list, self.inverter_client, command)

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

        # Labels match the WatchPower app on KING: QVFW2/QVFW3 are the two
        # SCC firmware versions (PDF wording differs). Bluetooth CPU comes
        # from VERFW:, not from QVFW2. Attribute names for QVFW2/QVFW3 stay
        # put so entity unique_ids are unchanged.
        sw_parts = []
        if client.cpu_firmware:
            sw_parts.append(f"CPU {client.cpu_firmware}")
        if client.bt_firmware:
            sw_parts.append(f"SCC1 {client.bt_firmware}")
        if client.panel_firmware:
            sw_parts.append(f"SCC2 {client.panel_firmware}")
        if getattr(client, "ble_firmware", ""):
            sw_parts.append(f"BT {client.ble_firmware}")
        if sw_parts:
            info["sw_version"] = " / ".join(sw_parts)

        if self.hub.device_key_source == "qid":
            info["serial_number"] = self._id

        return info
