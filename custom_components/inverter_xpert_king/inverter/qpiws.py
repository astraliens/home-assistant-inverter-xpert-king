"""Decoder for the QPIWS warning/fault bitfield.

Pure function: no Home Assistant imports, so it can be unit tested on its
own. Bit positions are `a0..aN`, left to right, matching how the vendor
documentation numbers them - `a0` is the first character right after the
opening `(`, not a bit-0/LSB convention.

Classification below follows the vendor documentation for this inverter
family. A bit with no reliably documented meaning is never given an
invented label - it is reported by its raw index only (or, if genuinely
contested between sources, left out of every list and surfaced only in
`unknown_bits`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_FAULT = "fault"
STATUS_UNKNOWN = "unknown"

# a0: informational only, never raises the status.
_INFO_BITS = frozenset({0})

# Unconditional faults.
_FAULT_BITS = frozenset({1, 2, 3, 4, 6, 7, 8, 18, 19, 20, 21, 22, 23, 24, 27, 30})

# Fault only while the general fault flag (a1) is also set, otherwise a warning.
_CONDITIONAL_FAULT_BITS = frozenset({9, 10, 11, 16})

# Warnings. 34/35 only exist on the full 36-bit reply.
_WARNING_BITS = frozenset({5, 12, 14, 15, 17, 25, 28, 29, 34, 35})

# Reserved on this model - set or not, it means nothing.
_RESERVED_BITS = frozenset({13})

# Not boolean flags on this model: decimal digits of a fault code instead.
# The two-digit code itself is read from a32a33.
_FAULT_CODE_DIGIT_BITS = frozenset({31, 32, 33})
_FAULT_CODE_START = 32
_FAULT_CODE_LEN = 2

_GENERAL_FAULT_BIT = 1

# Labels from the priority document for this model. Deliberately omitted:
# a13 (reserved), a26 (contested), a31-a33 (fault-code digits), a34-a35
# (other-model bits). Unlabelled set bits keep their index form.
_BIT_LABELS = {
    0: "PV loss",
    1: "Inverter fault",
    2: "Bus over",
    3: "Bus under",
    4: "Bus soft fail",
    5: "Line fail",
    6: "OPV short",
    7: "Inverter voltage too low",
    8: "Inverter voltage too high",
    9: "Over temperature",
    10: "Fan locked",
    11: "Battery voltage high",
    12: "Battery low alarm",
    14: "Battery under shutdown",
    15: "Battery derating",
    16: "Over load",
    17: "EEPROM",
    18: "Inverter over current",
    19: "Inverter soft fail",
    20: "Self test fail",
    21: "Output DC voltage over",
    22: "Battery open",
    23: "Current sensor fail",
    24: "Battery short",
    25: "Power limit",
    27: "MPPT overload fault",
    28: "MPPT overload warning",
    29: "Battery too low to charge",
    30: "DC/DC over current",
}

# Fault-code text from the priority document. Codes outside this table
# are left without a description.
_FAULT_DESCRIPTIONS = {
    1: "Fan is locked",
    2: "Over temperature",
    3: "Battery voltage is too high",
    4: "Battery voltage is too low",
    5: "Output short circuited or over temperature",
    6: "Output voltage is too high",
    7: "Over load time out",
    8: "Bus voltage is too high",
    9: "Bus soft start failed",
    10: "PV charger current over",
    51: "Over current inverter",
    53: "Inverter soft start failed",
    55: "Over DC voltage on output of inverter",
    57: "Current sensor failed",
    58: "Output voltage is too low",
    59: "PV voltage high",
    60: "Inverter negative power",
    71: "Parallel version different",
    72: "Output circuit failed",
    80: "CAN communication failed",
    81: "Parallel host line lost",
    82: "Parallel synchronized signal lost",
    83: "Parallel battery voltage detect different",
    84: "Parallel line voltage or frequency detect different",
    85: "Parallel line input current unbalanced",
    86: "Parallel output setting different",
}


def _bit_label(index: int) -> str:
    return _BIT_LABELS.get(index, f"a{index}")


def _is_readable_payload(raw: str) -> bool:
    """Flag positions must be 0/1; fault-code digit positions are exempt."""
    for index, ch in enumerate(raw):
        if index in _FAULT_CODE_DIGIT_BITS:
            continue
        if ch not in "01":
            return False
    return True


@dataclass
class WarningStatus:
    status: str
    summary: str
    faults: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    fault_code: int | None = None
    fault_description: str | None = None
    unknown_bits: list[int] = field(default_factory=list)
    raw: str = ""
    bits_length: int = 0


def _unreadable(raw: str) -> WarningStatus:
    return WarningStatus(status=STATUS_UNKNOWN, summary="", raw=raw, bits_length=len(raw))


def decode_warning_status(raw: str | None) -> WarningStatus:
    """Decode a QPIWS bit string into a health summary.

    Anything shorter or longer than the documented 36 bits is handled
    without raising: bits past the end of the string are simply not
    evaluated, and indices beyond the known table land in `unknown_bits`
    rather than being guessed at.
    """
    raw = raw or ""
    if not raw or not _is_readable_payload(raw):
        return _unreadable(raw)

    general_fault = len(raw) > _GENERAL_FAULT_BIT and raw[_GENERAL_FAULT_BIT] == "1"

    faults: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    unknown_bits: list[int] = []

    for index, bit in enumerate(raw):
        if bit != "1":
            continue
        if index in _RESERVED_BITS or index in _FAULT_CODE_DIGIT_BITS:
            continue
        if index in _INFO_BITS:
            info.append(_bit_label(index))
        elif index in _FAULT_BITS:
            faults.append(_bit_label(index))
        elif index in _CONDITIONAL_FAULT_BITS:
            (faults if general_fault else warnings).append(_bit_label(index))
        elif index in _WARNING_BITS:
            warnings.append(_bit_label(index))
        else:
            unknown_bits.append(index)

    fault_code = None
    fault_description = None
    code = raw[_FAULT_CODE_START : _FAULT_CODE_START + _FAULT_CODE_LEN]
    if len(code) == _FAULT_CODE_LEN and code.isdigit():
        parsed = int(code)
        # Vendor table has no code 0; "00" means no fault, not a real code.
        if parsed != 0:
            fault_code = parsed
            fault_description = _FAULT_DESCRIPTIONS.get(fault_code)

    if faults:
        status = STATUS_FAULT
    elif warnings:
        status = STATUS_WARNING
    else:
        status = STATUS_OK

    active = faults + warnings + info
    if fault_description is not None:
        active = active + [fault_description]
    summary = ", ".join(active) if active else "OK"

    return WarningStatus(
        status=status,
        summary=summary,
        faults=faults,
        warnings=warnings,
        info=info,
        fault_code=fault_code,
        fault_description=fault_description,
        unknown_bits=unknown_bits,
        raw=raw,
        bits_length=len(raw),
    )
