"""HID report descriptor parsing and a watcher for bits a report never declared.

Used by `doctor buttons --raw`. Some controllers put a button into bits their
descriptor marks as padding; the kernel then cannot expose it through evdev, but
it is plainly visible in the raw input report. This module only decodes bytes that
were read elsewhere: it performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

USAGE_PAGE_BUTTON = 0x09
_MAIN, _GLOBAL, _LOCAL = 0, 1, 2
_INPUT, _COLLECTION, _END_COLLECTION = 0x8, 0xA, 0xC
_USAGE_PAGE, _REPORT_SIZE, _REPORT_ID, _REPORT_COUNT, _PUSH, _POP = 0x0, 0x7, 0x8, 0x9, 0xA, 0xB
_USAGE, _USAGE_MIN, _USAGE_MAX = 0x0, 0x1, 0x2
_CONSTANT = 0x01
MAX_REPORT_BITS = 8 * 4096
# Bounds on what one raw report costs to decode and what one test run keeps: real gamepad reports
# are tens of bytes, and a handful of undeclared bits is already the finding.
MAX_REPORT_BYTES = 512
MAX_REPORT_IDS = 16
MAX_BITS_PER_REPORT = 32
MAX_BITS_TOTAL = 256


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


def parse_report_descriptor(data: bytes) -> list[Field]:
    """Input fields of a HID report descriptor, in report order. Malformed input ends the walk."""
    fields: list[Field] = []
    state = {"page": 0, "size": 0, "count": 0, "report_id": 0}
    stack: list[dict[str, int]] = []
    offsets: dict[int, int] = {}
    usages: list[int] = []
    usage_min = usage_max = 0
    index = 0
    while index < len(data):
        prefix = data[index]
        if prefix == 0xFE:  # long item: skip, nothing in it describes a field
            if index + 2 >= len(data):
                break
            index += 3 + data[index + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        kind, tag = (prefix >> 2) & 0x03, prefix >> 4
        payload = data[index + 1:index + 1 + size]
        if len(payload) < size:
            break
        value = int.from_bytes(payload, "little")
        index += 1 + size
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
