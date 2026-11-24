"""What an ASUS ROG Xbox Ally (RC73YA) says about its battery, for the tests that stand in for it.

Two places say it: UPower's DisplayDevice over D-Bus, and ``/sys/class/power_supply``. Both are the layouts of the
real device, minus anything that identifies it (it has no serial number worth copying here).
"""

from __future__ import annotations

from pathlib import Path


def rc73ya(**changes) -> dict:
    """The DisplayDevice's properties as UPower reports them with the machine running on its battery."""
    values = {"IsPresent": True, "Percentage": 78.0, "State": 2, "Type": 2, "TimeToEmpty": 28173, "TimeToFull": 0,
              "IconName": "battery-full-symbolic", "Energy": 42.3, "EnergyRate": 5.4, "UpdateTime": 1795502080,
              "WarningLevel": 1}
    values.update(changes)
    return values


def supply(root: Path, name: str, **files) -> Path:
    """One power_supply entry with the given attribute files."""
    directory = root / name
    directory.mkdir(parents=True)
    for attribute, text in files.items():
        (directory / attribute).write_text(f"{text}\n", encoding="utf-8")
    return directory


def rc73ya_tree(root: Path, capacity=78, status="Discharging") -> Path:
    """The mains adapter, the battery and the two USB ports of the ROG Xbox Ally.

    The battery is BAT1, not BAT0, and the USB ports are not batteries: their scope is Device.
    """
    supply(root, "ACAD", type="Mains", online="0")
    supply(root, "BAT1", type="Battery", status=status, capacity=capacity, capacity_level="Normal", present="1",
           technology="Li-ion")
    for port in ("001", "002"):
        supply(root, f"ucsi-source-psy-USBC000:{port}", type="USB", scope="Device", status="Not charging",
               online="0")
    return root
