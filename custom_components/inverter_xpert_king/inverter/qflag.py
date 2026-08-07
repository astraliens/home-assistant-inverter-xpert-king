"""Decoder for the QFLAG enabled/disabled feature-letter string.

Pure function: no Home Assistant imports. The wire format is
`E<enabled letters>D<disabled letters>` - `E`/`D` are section markers, not
letters themselves, and the letters within a section are a set: order
carries no meaning.

A letter that appears in neither section means the device simply doesn't
report it - that is not the same thing as "disabled", and the two must
stay distinguishable in the decoded result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Full names for attributes. 'm' is deliberately a status label, not a
# switch name - see _TOGGLE_LETTERS below.
_FULL_NAMES = {
    "a": "Buzzer alarm",
    "b": "Overload bypass",
    "c": "Overload bypass 2",
    "d": "Solar feed to grid",
    "j": "Power saving",
    "k": "LCD timeout",
    "m": "Battery connect status",
    "u": "Overload auto restart",
    "v": "Over temperature auto restart",
    "x": "Backlight",
    "y": "Source alarm",
    "z": "Fault log",
}

# Stable machine keys for the `flags` attribute. Independent of wording.
_FLAG_KEYS = {
    "a": "buzzer",
    "b": "overload_bypass",
    "c": "overload_bypass_2",
    "d": "solar_feed_to_grid",
    "j": "power_saving",
    "k": "lcd_timeout",
    "u": "overload_restart",
    "v": "over_temperature_restart",
    "x": "backlight",
    "y": "source_alarm",
    "z": "fault_log",
}

# Short labels for the entity state text - brief enough that even every
# known feature enabled at once stays well under the HA state length limit.
_SHORT_LABELS = {
    "a": "Buzzer alarm",
    "b": "Overload bypass",
    "c": "Bypass 2",
    "d": "Solar feed",
    "j": "Power saving",
    "k": "LCD timeout",
    "u": "Overload auto restart",
    "v": "Over temp auto restart",
    "x": "Backlight",
    "y": "Source alarm",
    "z": "Fault log",
}

_ALL_KNOWN_LETTERS = frozenset(_FULL_NAMES)
_BATTERY_LETTER = "m"
# Every known letter except the battery-status one behaves like a toggle.
_TOGGLE_LETTERS = _ALL_KNOWN_LETTERS - {_BATTERY_LETTER}

# The only letters PE/PD may ever be sent for. Power saving ('j') has no
# documented write behaviour, and 'm' is a status flag the device doesn't
# accept writes for. This is the single source of truth the write path
# checks against, so neither can be sent by mistake.
WRITABLE_LETTERS = frozenset({"a", "b", "c", "k", "u", "v", "x", "y", "z"})


def letter_to_key(letter: str) -> str | None:
    """Stable snake_case key for a known toggle letter, or None.

    None covers both an unknown letter and 'm' (battery-connect status,
    not a toggle) - callers that need to tell those apart should check
    membership in `_ALL_KNOWN_LETTERS`/`_TOGGLE_LETTERS` separately.
    """
    return _FLAG_KEYS.get(letter)

_STATE_CHAR_LIMIT = 255
_NONE_LABEL = "None"


@dataclass
class FlagStatus:
    state: str
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    not_reported: list[str] = field(default_factory=list)
    unknown_letters: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)
    battery_connected: bool | None = None
    raw: str = ""


def _malformed(raw: str) -> FlagStatus:
    """Safe fallback when the E/D markers can't be found - nothing is
    claimed to be enabled or disabled, every known letter is simply
    unreported."""
    return FlagStatus(
        state=_NONE_LABEL,
        not_reported=[_FULL_NAMES[letter] for letter in sorted(_ALL_KNOWN_LETTERS)],
        raw=raw,
    )


def format_state_text(labels: list[str], limit: int = _STATE_CHAR_LIMIT) -> str:
    """Join short labels with ', ', never exceeding `limit` characters.

    Instead of cutting a label in half, drops whole labels from the end
    and appends a "+N more" tail - this holds for any number or length of
    labels, not just the fixed set QFLAG happens to describe today.
    """
    if not labels:
        return _NONE_LABEL

    result = ""
    for position, label in enumerate(labels):
        candidate = f"{result}, {label}" if result else label
        remaining_after = len(labels) - position - 1
        tail = f" +{remaining_after} more" if remaining_after else ""
        if len(candidate) + len(tail) > limit:
            leftover = len(labels) - position
            cut_tail = f" +{leftover} more"
            return (result + cut_tail) if result else cut_tail.strip()
        result = candidate
    return result


def decode_flag_status(raw: str | None) -> FlagStatus:
    """Decode a QFLAG `E<letters>D<letters>` string.

    Tolerant of stray input: only lowercase a-z characters inside each
    section count as feature letters, so unexpected bytes or a missing
    marker never raise - they just leave less to report.
    """
    raw = raw or ""
    e_index = raw.find("E")
    d_index = raw.find("D")
    if e_index == -1 or d_index == -1 or d_index <= e_index:
        return _malformed(raw)

    enabled_letters = {ch for ch in raw[e_index + 1 : d_index] if "a" <= ch <= "z"}
    disabled_letters = {ch for ch in raw[d_index + 1 :] if "a" <= ch <= "z"}
    # A letter can only mean one thing; if the reply contradicts itself,
    # treat it as enabled rather than dropping it or raising.
    disabled_letters -= enabled_letters

    reported = enabled_letters | disabled_letters
    unknown_letters = sorted(reported - _ALL_KNOWN_LETTERS)

    known_enabled = enabled_letters & _TOGGLE_LETTERS
    known_disabled = disabled_letters & _TOGGLE_LETTERS

    enabled_names = [_FULL_NAMES[letter] for letter in sorted(known_enabled)]
    disabled_names = [_FULL_NAMES[letter] for letter in sorted(known_disabled)]
    not_reported = [
        _FULL_NAMES[letter] for letter in sorted(_ALL_KNOWN_LETTERS) if letter not in reported
    ]

    flags = {_FLAG_KEYS[letter]: True for letter in known_enabled}
    flags.update({_FLAG_KEYS[letter]: False for letter in known_disabled})

    if _BATTERY_LETTER in enabled_letters:
        battery_connected: bool | None = True
    elif _BATTERY_LETTER in disabled_letters:
        battery_connected = False
    else:
        battery_connected = None

    short_labels = [_SHORT_LABELS[letter] for letter in sorted(known_enabled)]

    return FlagStatus(
        state=format_state_text(short_labels),
        enabled=enabled_names,
        disabled=disabled_names,
        not_reported=not_reported,
        unknown_letters=unknown_letters,
        flags=flags,
        battery_connected=battery_connected,
        raw=raw,
    )
