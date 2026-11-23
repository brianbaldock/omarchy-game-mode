"""Device model identification from DMI, and the handhelds the doctor has advice for.

Only model identifiers are read. The serial numbers and UUIDs that live in the same
sysfs directory are never opened.
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import Context

DMI = "/sys/class/dmi/id"
DMI_FIELDS = ("sys_vendor", "product_name", "product_family", "board_name", "board_vendor", "bios_version")
PLACEHOLDERS = ("to be filled", "default string", "system product name", "system manufacturer", "not specified",
                "not applicable", "o.e.m.")


@dataclass(frozen=True)
class Handheld:
    name: str
    field: str
    values: tuple[str, ...]
    vendor: str
    verified: bool = False


# DMI matches taken from InputPlumber's device definitions
# (ShadowBlip/InputPlumber, rootfs/usr/share/inputplumber/devices/50-*.yaml, checked against 0.82.0).
# `verified` marks hardware this project has physically tested; the rest is listed, not tested.
HANDHELDS = (
    Handheld("ASUS ROG Xbox Ally", "board_name", ("RC73YA",), "asustek", verified=True),
    Handheld("ASUS ROG Xbox Ally X", "board_name", ("RC73XA",), "asustek"),
    Handheld("ASUS ROG Ally", "board_name", ("RC71L",), "asustek"),
    Handheld("ASUS ROG Ally X", "board_name", ("RC72LA",), "asustek"),
    Handheld("Lenovo Legion Go", "product_name", ("83E1",), "lenovo"),
    Handheld("Lenovo Legion Go S", "product_name", ("83L3", "83N6", "83Q2", "83Q3"), "lenovo"),
    Handheld("Lenovo Legion Go 2", "product_name", ("83N0", "83N1"), "lenovo"),
    Handheld("Valve Steam Deck", "product_name", ("Jupiter", "Galileo"), "valve"),
    Handheld("MSI Claw", "board_name", ("MS-1T41", "MS-1T42", "MS-1T52"), "micro-star"),
)


def dmi(ctx: Context) -> dict[str, str]:
    def read() -> dict[str, str]:
        values = {}
        for name in DMI_FIELDS:
            value = ctx.fs.line(f"{DMI}/{name}")[:80]
            values[name] = "" if any(word in value.lower() for word in PLACEHOLDERS) else value
        return values
    return ctx.once("dmi", read)


def known_handheld(values: dict[str, str]) -> Handheld | None:
    vendors = f"{values.get('sys_vendor', '')} {values.get('board_vendor', '')}".lower()
    for handheld in HANDHELDS:
        if values.get(handheld.field, "") in handheld.values and handheld.vendor in vendors:
            return handheld
    return None
