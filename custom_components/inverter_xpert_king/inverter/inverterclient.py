import json
import logging
import socket
import time
from datetime import datetime
from os import path
from textwrap import wrap

from ..const import VERIFY_RESPONSE_CRC
from .crc16pureclass import CRC16Calc

_LOGGER = logging.getLogger(__name__)

SOCKET_TIMEOUT = 10
# Real frames are well under 200 bytes; anything past this without a
# terminator means the stream is desynchronised, not just slow.
MAX_FRAME_BYTES = 512
# Shared rate limit for anomaly logging (CRC mismatches, field parse
# failures, stale frames) so a persistent condition logs occasionally
# instead of once per poll.
CRC_MISMATCH_LOG_INTERVAL = 60

# A CRC byte equal to one of the frame delimiters is transmitted as itself + 1,
# otherwise it would be indistinguishable from the start/end of the frame.
CRC_BUMP_BYTES = frozenset({0x28, 0x0A, 0x0D})  # '(' , LF, CR


class InverterCommError(Exception):
    """Transport-level failure talking to the inverter (closed link, no terminator, ...)."""


def _produced_field_names(field_conf) -> list[str]:
    """Field names one command-table entry would contribute if parsed.

    A plain field just produces its own name. A bitfield
    (`string_binary`/`assoc_value`) decodes into one entry per sub-field in
    `complex_params.params` — the outer name is never emitted on its own for
    those types, so a failure while decoding one of them means none of its
    sub-fields have a value this round, not that the outer field is absent.
    """
    if field_conf.get("type") in ("string_binary", "assoc_value"):
        names = (p.get("name") for p in field_conf.get("complex_params", {}).get("params", []))
        return [name for name in names if name]
    name = field_conf.get("name")
    return [name] if name else []


def wire_decimal_places(raw: str) -> int:
    """Digits after the decimal point in a protocol field token, else 0."""
    if "." not in raw:
        return 0
    return len(raw.split(".", 1)[1])


def escape_crc_bytes(crc_int):
    """Apply the wire CRC escaping rule to a computed CRC16 value."""
    hi = (crc_int >> 8) & 0xFF
    lo = crc_int & 0xFF
    if hi in CRC_BUMP_BYTES:
        hi = (hi + 1) & 0xFF
    if lo in CRC_BUMP_BYTES:
        lo = (lo + 1) & 0xFF
    return bytes((hi, lo))


class InverterClient:
    def __init__(self, device_ip: str, device_port: int):
        self.device_ip = device_ip
        self.device_port = device_port
        self.connected = False

        self.model_general = ''
        self.model = ''
        self.cpu_firmware = ''
        self.panel_firmware = ''
        self.bt_firmware = ''
        # Bluetooth CPU from VERFW: — separate from bt_firmware (QVFW2 / SCC1).
        self.ble_firmware = ''

        self._command_defs = None
        self.crc_mismatch_count = 0
        self._last_crc_warning = 0.0
        # Extra fields beyond the command table are a normal model difference,
        # not a fault — log each (command, index) at most once.
        self._logged_undescribed = set()
        self._last_parse_warning = {}
        # Names process_data could not convert on its most recent call - a
        # field that arrived but failed to parse, as opposed to one that
        # was never in the reply at all. Reset at the start of every call.
        self.last_field_failures: set[str] = set()

        # Bytes past the frame _read_frame is currently returning. In
        # practice this resolves to empty before each call returns (the
        # trailing-tail wait in _read_frame either completes it or gives
        # up and discards it), but it's still cleared on every
        # connect()/disconnect() defensively — leftover bytes must never
        # cross a reconnect.
        self._frame_leftover = b''
        self.stale_frame_count = 0
        self._last_stale_frame_log = 0.0

    def connect(self):
        # Keep an already-open socket. Opening another would orphan the
        # descriptor currently held in client_socket.
        if self.connected and getattr(self, "client_socket", None) is not None:
            return 1

        sock = None
        try:
            sock = socket.socket()
            sock.settimeout(SOCKET_TIMEOUT)
            sock.connect((self.device_ip, self.device_port))
        except OSError as exc:
            _LOGGER.debug("Connect to %s:%s failed: %s", self.device_ip, self.device_port, exc)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.connected = False
            return 0

        self.client_socket = sock
        self.connected = True
        # A new socket starts with a clean slate — any leftover bytes
        # belonged to the previous connection's stream, not this one.
        self._frame_leftover = b''
        self._read_identity()
        return 1

    def _read_identity(self):
        """Read model/firmware right after connect. Never raises, and each
        field is read on its own: a device refusing one command (QVFW2 is
        KING-only and NAKs on other models; VERFW: is absent on most of
        the family) must not cost the others."""
        self.model_general = self._read_identity_field(self.get_model2)
        self.model = self._read_identity_field(self.get_model)
        self.cpu_firmware = self._read_identity_field(self.get_cpu_firmware_version)
        self.panel_firmware = self._read_identity_field(self.get_panel_firmware_version)
        self.bt_firmware = self._read_identity_field(self.get_bt_version)
        self.ble_firmware = self._read_identity_field(self.get_ble_firmware_version)

    def _read_identity_field(self, getter):
        try:
            return getter()
        except Exception as exc:
            _LOGGER.debug("Could not read inverter identity field: %s", exc)
            return ''

    def disconnect(self):
        sock = getattr(self, 'client_socket', None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self.connected = False
        self._frame_leftover = b''
        return 1

    def reconnect(self):
        """Reconnect the socket. Must never raise, even if the device
        replies with garbage or nothing at all right after connecting."""
        try:
            self.disconnect()
            self.connect()
        except Exception as exc:
            _LOGGER.warning("Reconnect to %s:%s failed: %s", self.device_ip, self.device_port, exc)
            self.connected = False

    def calc_crc(self, command):
        crc_calc = CRC16Calc()
        crc_int = crc_calc.crc16xmodem(command)
        return escape_crc_bytes(crc_int)

    def _read_frame(self):
        """Read one frame, assembling a response that may arrive in several
        TCP segments.

        Commands are strictly serialised (one outstanding request at a
        time, device never sends unsolicited data), so any extra complete
        frame found alongside the one we're looking for is a stale reply
        to an earlier, already-abandoned command — not data we should ever
        hand back to the caller. We keep only the most recent complete
        frame: it is the one most likely to answer the command we
        just sent.

        A complete frame followed by trailing partial bytes is itself
        evidence that the complete frame is stale — the real reply is
        still arriving behind it. So instead of returning right away, we
        keep reading for that tail to complete, bounded by the same socket
        timeout and MAX_FRAME_BYTES guard as any other read, and prefer
        whatever completes over what we already have. If the tail never
        completes we fall back to the newest complete frame in hand rather
        than raising — we already have a usable answer. The common case,
        exactly one frame with nothing after it, returns immediately with
        no added latency.
        """
        buf = bytearray(self._frame_leftover)
        self._frame_leftover = b''

        def _extract_frames():
            found = []
            while True:
                term = buf.find(b'\r')
                if term == -1:
                    return found
                found.append(bytes(buf[:term]))
                del buf[:term + 1]

        while b'\r' not in buf:
            chunk = self.client_socket.recv(256)
            if not chunk:
                raise InverterCommError("connection closed by peer")
            buf += chunk
            if len(buf) > MAX_FRAME_BYTES:
                raise InverterCommError(f"frame exceeded {MAX_FRAME_BYTES} bytes without a terminator")

        frames = _extract_frames()

        while buf:
            try:
                chunk = self.client_socket.recv(256)
            except OSError:
                buf.clear()
                break
            if not chunk:
                buf.clear()
                break
            buf += chunk
            if len(buf) > MAX_FRAME_BYTES:
                buf.clear()
                break
            frames.extend(_extract_frames())

        self._frame_leftover = bytes(buf)
        if len(frames) > 1:
            self._note_stale_frames(frames[:-1])
        return frames[-1]

    def _note_stale_frames(self, stale_frames):
        """A stale frame is a model/gateway quirk, not something a user
        needs to see as an error — debug only, rate-limited."""
        self.stale_frame_count += len(stale_frames)
        now = time.monotonic()
        if now - self._last_stale_frame_log >= CRC_MISMATCH_LOG_INTERVAL:
            _LOGGER.debug(
                "dropped %d stale frame(s) ahead of the current reply (raw=%r); %d total",
                len(stale_frames), stale_frames, self.stale_frame_count,
            )
            self._last_stale_frame_log = now

    def _split_frame(self, frame):
        """Split a CR-stripped frame into decoded text, the raw CRC body
        (everything the CRC was computed over) and the two CRC bytes,
        taken by position so a whitespace-looking CRC byte can't eat data."""
        if len(frame) < 3:
            raise InverterCommError(f"frame too short: {frame!r}")
        body, crc_bytes = frame[:-2], frame[-2:]
        payload = body[1:] if body[:1] == b'(' else body
        return payload.decode('latin1').strip(), body, crc_bytes

    def _verify_crc(self, command, body, crc_bytes):
        expected = CRC16Calc().crc16xmodem(body.decode('latin1'))
        expected_wire = escape_crc_bytes(expected)
        expected_raw = expected.to_bytes(2, 'big')
        # Our device always escapes per the CRC_BUMP_BYTES rule, but other
        # firmware may send the raw CRC unescaped; accept either. The two
        # forms only differ when the CRC contains a byte that gets bumped.
        if crc_bytes in (expected_wire, expected_raw):
            return True

        self.crc_mismatch_count += 1
        now = time.monotonic()
        if now - self._last_crc_warning >= CRC_MISMATCH_LOG_INTERVAL:
            _LOGGER.warning(
                "CRC mismatch on %s response (got %s, expected %s); %d mismatches so far",
                command, crc_bytes.hex(), expected_wire.hex(), self.crc_mismatch_count,
            )
            self._last_crc_warning = now
        return False

    def get_data(self, command, param=''):
        if not self.connected:
            self.connect()
            if not self.connected:
                return []

        match command:
           case 'QLD':
            if(param==''): # if total energy per day without date set current date
                cur_date=datetime.today().strftime('%Y%m%d')
                param=cur_date

        request = command + param
        message = bytes(request, 'latin1') + self.calc_crc(request) + bytes([13])

        try:
            self.client_socket.send(message)
            frame = self._read_frame()
        except (OSError, InverterCommError) as exc:
            _LOGGER.debug("get_data(%s) transport error: %s", command, exc)
            self.disconnect()
            return []

        try:
            text, body, crc_bytes = self._split_frame(frame)
        except InverterCommError as exc:
            _LOGGER.debug("get_data(%s) malformed frame: %s", command, exc)
            return []

        if VERIFY_RESPONSE_CRC and not self._verify_crc(command, body, crc_bytes):
            return []

        if text in ('NAK', 'ACK'):
            return [text]

        # A value in the last described field can itself contain a space
        # (e.g. a model name in QMN) and must not be split further. But a
        # device answering with more fields than the table describes is a
        # normal model difference — those surplus values must not be glued
        # onto the last field either. Token count alone can't tell the two
        # apart, so use the field's declared type: only a free-text
        # ("string") last field is bounded to `maxsplit`; anything else
        # (numeric, bit-string, ...) is split on every space, so surplus
        # values fall out as their own extra, undescribed tokens that
        # process_data already skips harmlessly.
        command_params = self.GetCommandList().get(command, {})
        last_index = max((int(key) for key in command_params), default=-1)
        last_field_type = command_params.get(str(last_index), {}).get('type') if last_index >= 0 else None
        data_array = text.split(' ', last_index) if last_field_type == 'string' else text.split(' ')

        match command:
            case "QVFW" | "QVFW3" | "VERFW:" | "VERFW":
              data_array[0] = data_array[0].replace("VERFW:", "")
            case "QVFW2":
              data_array[0] = data_array[0].replace("VERFW2:", "")

        return data_array

    def GetCommandList(self):
        if self._command_defs is None:
            basepath = path.dirname(__file__)
            filepath = path.join(basepath, "inverter_commands.json")
            with open(filepath, encoding='utf-8') as data_file:
                self._command_defs = json.load(data_file)
        return self._command_defs

    def GetCommandByType(self,command_type):
        command_data=self.GetCommandList()
        command_list={
               'data':['QPIGS','QMOD','QPIWS','QFLAG','QET','QLT','QLD','QPIGS2'],
               'conf':['QID','QGMN','QPIRI','QVFW','QVFW3','QVFW2','VERFW:','QMN']
        }

        command_result={}
        for command in command_list[command_type]:
           if command in command_data:
            command_result[command]=command_data[command]

        return command_result

    def GetInverterDataByCommandSync(self,command_type):
        res=[]
        command_list=self.GetCommandByType(command_type)

        for command in command_list:
            inverter_data=self.get_data(command)
            received_data=self.process_data(command,inverter_data)
            for received_item in received_data:
                res.append(received_item)
        return res

    def process_data(self, command, inverter_data):
        self.last_field_failures = set()
        if not inverter_data:
            return []
        if inverter_data == ['NAK']:
            _LOGGER.debug("%s refused by device (NAK)", command)
            return []

        res = []
        command_params = self.GetCommandList().get(command, {})

        for index in range(len(inverter_data)):
            index_str = str(index)
            if index_str not in command_params:
                # The command table covers the protocol family, not one model;
                # an undescribed index just means this device doesn't send
                # that field. Not a fault, so at most debug, and only once
                # per (command, index) — otherwise this repeats every poll.
                undescribed_key = (command, index)
                if undescribed_key not in self._logged_undescribed:
                    self._logged_undescribed.add(undescribed_key)
                    _LOGGER.debug(
                        "%s: no field defined for index %d (raw=%r)", command, index, inverter_data
                    )
                continue

            field_conf = command_params[index_str]
            try:
                field_name = field_conf['name']
                field_type = field_conf['type']
                field_unit = field_conf.get('unit', '')
                field_sensor = field_conf.get('sensor', '')
                field_text = field_conf.get('text', field_name)

                decimals = None
                match field_type:
                    case "int":
                        field_val=int(inverter_data[index])
                        if 'multiplier' in field_conf:
                            field_val=field_val*field_conf['multiplier']
                        decimals = 0

                    case "float":
                        raw_token = inverter_data[index]
                        field_val=float(raw_token)
                        if 'multiplier' in field_conf:
                            field_val=field_val*field_conf['multiplier']
                        decimals = wire_decimal_places(raw_token)

                    case "string":
                        field_val=inverter_data[index]

                    case 'string_binary':
                        field_val=inverter_data[index]
                        parsed_array_val=self.parse_params_string_binary(field_val, field_conf['complex_params']['params'])

                    case 'assoc_value':
                        field_val=inverter_data[index]
                        parsed_array_val=self.parse_params_assoc_value(field_val, field_conf['complex_params']['params'])

                    case _:
                        field_val=inverter_data[index]

                match field_type:
                    case 'string_binary' | 'assoc_value':
                        for parsed_item in parsed_array_val:
                            res.append({
                                'param': parsed_item['name'],
                                'value': parsed_item['value'],
                                'unit': '',
                                'command': command,
                                'sensor': parsed_item['sensor'],
                                'text': parsed_item.get('text', parsed_item['name']),
                            })

                    case _:
                        item = {
                            'param': field_name,
                            'value': field_val,
                            'unit': field_unit,
                            'command': command,
                            'sensor': field_sensor,
                            'text': field_text,
                        }
                        if decimals is not None:
                            item['decimals'] = decimals
                        res.append(item)

            except Exception as exc:
                # A field that arrived but failed to parse is a genuine
                # anomaly, unlike an undescribed index — keep it a warning,
                # but rate-limited so a persistently bad field doesn't spam.
                # It also proves nothing about whether the device supports
                # the field, so callers must not treat it as absence.
                self.last_field_failures.update(_produced_field_names(field_conf))
                warning_key = (command, index)
                now = time.monotonic()
                if now - self._last_parse_warning.get(warning_key, 0.0) >= CRC_MISMATCH_LOG_INTERVAL:
                    _LOGGER.warning(
                        "%s: failed to parse field %d (raw=%r): %s", command, index, inverter_data[index], exc
                    )
                    self._last_parse_warning[warning_key] = now

        return res

    def parse_params_string_binary(self, params, param_conf):
        params_array = wrap(params, 1)
        if len(params_array) != len(param_conf):
            _LOGGER.debug(
                "string_binary length mismatch: got %d char(s), expected %d (%r)",
                len(params_array), len(param_conf), params,
            )
        res=[]
        for index in range(len(params_array)):
            if index >= len(param_conf):
                break
            value = params_array[index]
            tpl = param_conf[index]
            field_sensor=''
            if 'sensor' in tpl:
                field_sensor=tpl['sensor']
            res.append({
                'name': tpl['name'],
                'text': tpl.get('text', tpl['name']),
                'value': tpl['value'][value],
                'value_orig': value,
                'sensor': field_sensor
            })
        return res

    def parse_params_assoc_value(self,params,param_conf):
        assoc_value=params
        res=[]
        
        assoc_data=param_conf[0]
        assoc_list=assoc_data['value']
        if assoc_value in assoc_list:
            assoc_result=assoc_list[assoc_value]
        else:
            assoc_result=assoc_value
        
        res.append({
            'name': assoc_data['name'],
            'text': assoc_data['text'],
            'value': assoc_result,
            'value_orig': assoc_value,
            'sensor': assoc_data['sensor']
        })

        return res

    def get_data_single_param(self,command):
        inverter_data=self.get_data(command)
        received_data=self.process_data(command,inverter_data)
        try:
            return received_data[0]['value']
        except IndexError:
            return ''

    def get_data_single_param_full(self,command):
        inverter_data=self.get_data(command)
        received_data=self.process_data(command,inverter_data)
        return received_data[0]
    
    def get_serial(self, full=False):
        # `full` is always a bool flag (default False); callers never pass
        # other truthy values through this path.
        if full:
            return self.get_data_single_param_full('QID')
        return self.get_data_single_param('QID')

    def get_model2(self, full=False):
        if full:
            return self.get_data_single_param_full('QGMN')
        return self.get_data_single_param('QGMN')

    def get_mode(self, full=False):
        if full:
            return self.get_data_single_param_full('QMOD')
        return self.get_data_single_param('QMOD')

    def get_warning_status(self, full=False):
        if full:
            return self.get_data_single_param_full('QPIWS')
        return self.get_data_single_param('QPIWS')

    def get_cpu_firmware_version(self, full=False):
        data = self.get_data_single_param_full('QVFW')
        if full:
            return data
        return data['value']

    def get_panel_firmware_version(self, full=False):
        data = self.get_data_single_param_full('QVFW3')
        if full:
            return data
        return data['value']

    def get_bt_version(self, full=False):
        if full:
            return self.get_data_single_param_full('QVFW2')
        return self.get_data_single_param('QVFW2')

    def get_ble_firmware_version(self, full=False):
        """Bluetooth CPU firmware (P1 §2.7 VERFW:). Optional: NAK, empty,
        or timeout → empty string, never an error. Documented form first;
        bare VERFW once as fallback (open-source framing variant)."""
        value = ''
        for command in ('VERFW:', 'VERFW'):
            data = self.get_data(command)
            if not data or data == ['NAK']:
                continue
            raw = data[0].replace('VERFW:', '').strip()
            if raw:
                value = raw
                break
        if full:
            return {
                'name': 'ble_cpu_firmware',
                'text': 'Bluetooth CPU firmware',
                'value': value,
                'value_orig': value,
                'sensor': 'info',
            }
        return value

    def get_flag_status(self, full=False):
        if full:
            return self.get_data_single_param_full('QFLAG')
        return self.get_data_single_param('QFLAG')

    def get_model(self, full=False):
        if full:
            return self.get_data_single_param_full('QMN')
        return self.get_data_single_param('QMN')

    def get_current_state(self):
        command = 'QPIGS'
        inverter_data = self.get_data(command)
        received_data = self.process_data(command, inverter_data)
        return received_data

    def get_current_conf(self):
        command = 'QPIRI'
        inverter_data = self.get_data(command)
        received_data = self.process_data(command, inverter_data)
        return received_data

    def get_energy_today(self):
        command = 'QLD'
        cur_date = datetime.today().strftime('%Y%m%d')
        cur_date_reset_time = datetime.today().strftime('%Y-%m-%d') + ' 00:00:00'
        inverter_data = self.get_data(command, cur_date)
        received_data = self.process_data(command, inverter_data)
        received_data[0]["last_reset"] = cur_date_reset_time
        return received_data[0]

    def get_energy_total(self):
        command = 'QLT'
        inverter_data = self.get_data(command)
        received_data = self.process_data(command, inverter_data)
        return received_data[0]
