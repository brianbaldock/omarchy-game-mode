"""HID report descriptor parsing and a watcher for bits a report never declared.

Used by `doctor buttons --raw`. Some controllers put a button into bits their
descriptor marks as padding; the kernel then cannot expose it through evdev, but
it is plainly visible in the raw input report. The doctor's vibration check uses
the descriptor's output reports to find a haptics report the kernel does not drive.
This module only decodes bytes that were read elsewhere: it performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

USAGE_PAGE_BUTTON = 0x09
USAGE_PAGE_PHYSICAL = 0x0F  # Physical Interface Device: force feedback and haptics
_MAIN, _GLOBAL, _LOCAL = 0, 1, 2
_INPUT, _OUTPUT, _COLLECTION, _END_COLLECTION = 0x8, 0x9, 0xA, 0xC
_USAGE_PAGE, _REPORT_SIZE, _REPORT_ID, _REPORT_COUNT, _PUSH, _POP = 0x0, 0x7, 0x8, 0x9, 0xA, 0xB
_LOGICAL_MIN, _LOGICAL_MAX = 0x1, 0x2
_USAGE, _USAGE_MIN, _USAGE_MAX = 0x0, 0x1, 0x2
_CONSTANT = 0x01
MAX_REPORT_BITS = 8 * 4096
# Bounds on what one raw report costs to decode and what one test run keeps: real gamepad reports
# are tens of bytes, and a handful of undeclared bits is already the finding.
MAX_REPORT_BYTES = 512
MAX_REPORT_IDS = 16
MAX_BITS_PER_REPORT = 32
MAX_BITS_TOTAL = 256
# Bounds on reading a descriptor for its output reports. The kernel keeps at most 4096 bytes of one,
# and a real descriptor holds a few hundred items.
MAX_DESCRIPTOR_BYTES = 4096
MAX_DESCRIPTOR_ITEMS = 1024
MAX_PUSH_DEPTH = 16
MAX_ITEM_USAGES = 64
ENABLE_ACTUATORS, MAGNITUDE, DURATION, START_DELAY, LOOP_COUNT = (
    USAGE_PAGE_PHYSICAL << 16 | usage for usage in (0x97, 0x70, 0x50, 0xA7, 0x7C))
# The Xbox One controller's rumble report, as (usages, size, count, logical minimum, logical maximum) per
# Output item: a 4-bit mask of the motors to drive and 4 bits of padding, the strength of each of the four
# motors in percent, then duration, start delay and loop count.
XBOX_HAPTICS = (((ENABLE_ACTUATORS,), 4, 1, 0, 1), ((), 4, 1, 0, 0), ((MAGNITUDE,), 8, 4, 0, 100),
                ((DURATION,), 8, 1, 0, 255), ((START_DELAY,), 8, 1, 0, 255), ((LOOP_COUNT,), 8, 1, 0, 255))


@dataclass(frozen=True)
class Field:
    """One Input item: `bits` bits starting `offset` bits after the report id byte."""

    report_id: int
    offset: int
    bits: int
    constant: bool
    usage_page: int
    usage_min: int = 0
    usage_max: int = 0

    @property
    def is_buttons(self) -> bool:
        return not self.constant and self.usage_page == USAGE_PAGE_BUTTON


def _items(data: bytes, limit: int | None = None) -> Iterator[tuple[int, int, bytes]]:
    """(type, tag, data) of each short item in order, one at a time; at most `limit` items are walked.

    Long items are skipped: nothing in them describes a field. A truncated item ends the walk.
    """
    index = walked = 0
    while index < len(data) and (limit is None or walked < limit):
        walked += 1
        prefix = data[index]
        if prefix == 0xFE:
            if index + 2 >= len(data):
                return
            index += 3 + data[index + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        payload = data[index + 1:index + 1 + size]
        if len(payload) < size:
            return
        index += 1 + size
        yield (prefix >> 2) & 0x03, prefix >> 4, payload


def parse_report_descriptor(data: bytes) -> list[Field]:
    """Input fields of a HID report descriptor, in report order. Malformed input ends the walk."""
    fields: list[Field] = []
    state = {"page": 0, "size": 0, "count": 0, "report_id": 0}
    stack: list[dict[str, int]] = []
    offsets: dict[int, int] = {}
    usages: list[int] = []
    usage_min = usage_max = 0
    for kind, tag, payload in _items(data):
        value = int.from_bytes(payload, "little")
        if kind == _GLOBAL:
            if tag == _USAGE_PAGE:
                state["page"] = value
            elif tag == _REPORT_SIZE:
                state["size"] = value
            elif tag == _REPORT_COUNT:
                state["count"] = value
            elif tag == _REPORT_ID:
                state["report_id"] = value
            elif tag == _PUSH:
                stack.append(dict(state))
            elif tag == _POP and stack:
                state = stack.pop()
        elif kind == _LOCAL:
            if tag == _USAGE:
                usages.append(value)
            elif tag == _USAGE_MIN:
                usage_min = value
            elif tag == _USAGE_MAX:
                usage_max = value
        elif kind == _MAIN:
            if tag == _INPUT:
                bits = state["size"] * state["count"]
                offset = offsets.get(state["report_id"], 0)
                if bits <= 0 or offset + bits > MAX_REPORT_BITS:
                    break
                low, high = usage_min, usage_max
                if not high and usages:
                    low, high = min(usages), max(usages)
                fields.append(Field(state["report_id"], offset, bits, bool(value & _CONSTANT), state["page"],
                                    low, high))
                offsets[state["report_id"]] = offset + bits
            if tag in (_INPUT, 0x9, 0xB, _COLLECTION, _END_COLLECTION):
                usages, usage_min, usage_max = [], 0, 0  # local items end at every main item
    return fields


def uses_report_ids(fields: list[Field]) -> bool:
    return any(item.report_id for item in fields)


@dataclass(frozen=True)
class OutputField:
    """One Output item: `count` values of `size` bits each. A usage holds its page in its high 16 bits."""

    report_id: int
    usages: tuple[int, ...]
    size: int
    count: int
    constant: bool
    minimum: int
    maximum: int

    @property
    def shape(self) -> tuple[tuple[int, ...], int, int, int, int]:
        """Usages, size, count and logical range; padding is padding whatever range it declares."""
        if self.constant:
            return (), self.size, self.count, 0, 0
        return self.usages, self.size, self.count, self.minimum, self.maximum


@dataclass(frozen=True)
class Haptics:
    """An output report that drives the controller's vibration motors."""

    report_id: int
    xbox_layout: bool  # laid out exactly like the Xbox One controller's rumble report


def output_fields(data: bytes) -> list[OutputField]:
    """Output items of a HID report descriptor, in order.

    Bounded whatever the bytes: only the first MAX_DESCRIPTOR_BYTES bytes and MAX_DESCRIPTOR_ITEMS items
    are looked at, one item at a time and without recursion. Long items are skipped, and a truncated
    item ends the walk with what was found before it.
    """
    fields: list[OutputField] = []
    state = {"page": 0, "size": 0, "count": 0, "report_id": 0, "minimum": 0, "maximum": 0}
    stack: list[dict[str, int]] = []
    usages: list[tuple[int, bool]] = []
    for kind, tag, payload in _items(data[:MAX_DESCRIPTOR_BYTES], MAX_DESCRIPTOR_ITEMS):
        value = int.from_bytes(payload, "little")
        if kind == _GLOBAL:
            if tag == _USAGE_PAGE:
                state["page"] = value
            elif tag == _LOGICAL_MIN:
                state["minimum"] = int.from_bytes(payload, "little", signed=True)
            elif tag == _LOGICAL_MAX:
                # Read as the kernel reads it: unsigned unless the minimum is negative.
                state["maximum"] = value if state["minimum"] >= 0 else int.from_bytes(payload, "little", signed=True)
            elif tag == _REPORT_SIZE:
                state["size"] = value
            elif tag == _REPORT_COUNT:
                state["count"] = value
            elif tag == _REPORT_ID:
                state["report_id"] = value
            elif tag == _PUSH and len(stack) < MAX_PUSH_DEPTH:
                stack.append(dict(state))
            elif tag == _POP and stack:
                state = stack.pop()
        elif kind == _LOCAL and tag == _USAGE and len(usages) < MAX_ITEM_USAGES:
            usages.append((value, len(payload) == 4))  # a 4-byte usage names its own page
        elif kind == _MAIN:
            if tag == _OUTPUT:
                full = tuple(usage if extended else state["page"] << 16 | usage for usage, extended in usages)
                fields.append(OutputField(state["report_id"], full, state["size"], state["count"],
                                          bool(value & _CONSTANT), state["minimum"], state["maximum"]))
            usages = []  # local items end at every main item
    return fields


def haptics_report(data: bytes) -> Haptics | None:
    """The output report that carries `Enable Actuators` and `Magnitude` of the Physical Interface page, if any."""
    fields = output_fields(data)
    for report_id in dict.fromkeys(item.report_id for item in fields):
        report = [item for item in fields if item.report_id == report_id]
        declared = {usage for item in report if not item.constant for usage in item.usages}
        if {ENABLE_ACTUATORS, MAGNITUDE} <= declared:
            return Haptics(report_id, tuple(item.shape for item in report) == XBOX_HAPTICS)
    return None


@dataclass(frozen=True)
class UndeclaredBit:
    report_id: int
    bit: int
    where: str


def describe_bit(fields: list[Field], report_id: int, bit: int) -> str:
    """Where an undeclared bit sits relative to the declared buttons of its report."""
    report = [item for item in fields if item.report_id == report_id]
    for item in report:
        if item.offset <= bit < item.offset + item.bits:
            before = next((other for other in report if other.is_buttons and other.offset + other.bits == item.offset),
                          None)
            if before is not None:
                number = before.usage_max + 1 + bit - item.offset if before.usage_max else before.bits + 1
                return (f"padding right after the {before.bits} declared buttons "
                        f"(where button {number} would be)")
            return "padding between declared fields"
    return "beyond the end of the declared report"


@dataclass
class RawWatcher:
    """Reports bits that change although the descriptor says they carry nothing.

    Decoding is bounded: the first MAX_REPORT_BYTES of a report, MAX_REPORT_IDS report ids,
    MAX_BITS_PER_REPORT new bits per report and MAX_BITS_TOTAL over the whole test.
    """

    fields: list[Field]
    _baseline: dict[int, tuple[int, int]] = field(default_factory=dict)
    _seen: set[tuple[int, int]] = field(default_factory=set)
    _declared: dict[int, int] = field(default_factory=dict)

    def _declared_mask(self, report_id: int) -> int:
        """The bits of a report that the descriptor declares as data, as one integer."""
        if report_id not in self._declared:
            self._declared[report_id] = sum(((1 << item.bits) - 1) << item.offset for item in self.fields
                                            if item.report_id == report_id and not item.constant)
        return self._declared[report_id]

    def feed(self, report: bytes) -> list[UndeclaredBit]:
        """Undeclared bits of `report` that differ from the first report seen, each reported once."""
        if not report:
            return []
        numbered = uses_report_ids(self.fields)
        report_id, payload = (report[0], report[1:]) if numbered else (0, report)
        payload = payload[:MAX_REPORT_BYTES]
        # Bit n of the report is bit n of this little-endian number, as in the descriptor's offsets.
        value = int.from_bytes(payload, "little")
        if report_id not in self._baseline:
            if len(self._baseline) < MAX_REPORT_IDS:
                self._baseline[report_id] = (value, len(payload))
            return []
        baseline, size = self._baseline[report_id]
        compared = (1 << 8 * min(size, len(payload))) - 1
        changed = (value ^ baseline) & compared & ~self._declared_mask(report_id)
        found: list[UndeclaredBit] = []
        while changed and len(found) < MAX_BITS_PER_REPORT and len(self._seen) < MAX_BITS_TOTAL:
            lowest = changed & -changed
            changed ^= lowest
            bit = lowest.bit_length() - 1
            if (report_id, bit) not in self._seen:
                self._seen.add((report_id, bit))
                found.append(UndeclaredBit(report_id, bit, describe_bit(self.fields, report_id, bit)))
        return found

    def summary(self) -> dict[str, int]:
        buttons = next((item for item in self.fields if item.is_buttons), None)
        return {"report_id": buttons.report_id if buttons else 0, "declared_buttons": buttons.bits if buttons else 0}
