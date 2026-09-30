"""The "extras" master switches — Settings → Services.

`[Services] <name> = true|false` decides whether an optional feature exists
at all: its Settings section is listed, its dock pill / page shows, and its
process runs. Off means off everywhere — the feature's own `enabled` flag is
ANDed with this, so nothing it owns starts at boot either.

    dashcam        the 70mai (Nav 7, [Dashcam])
    actioncam      GoPro / Insta360 (Nav 8, [ActionCam])
    airphone       AirPlay receiver (Nav 3, rpiplay, [AirPlay])
    steering_keys  the car's Bluetooth buttons (media_keys listener, [SteeringKeys])

Defaults are on, so a settings.ini from before this existed behaves as it did.
"""
from __future__ import annotations

from Modules.Runtime import settings_cache
from preferences import DATA_SETTINGS

SERVICES = ("dashcam", "actioncam", "airphone", "steering_keys")
LABELS = {
    "dashcam": "70mai dashcam",
    "actioncam": "Action cam (GoPro / Insta360)",
    "airphone": "AirPhone (AirPlay receiver)",
    "steering_keys": "Steering keys",
}


def service_on(name: str) -> bool:
    if name not in SERVICES:
        return True
    return settings_cache.getboolean("Services", name, fallback=True, path=DATA_SETTINGS)


def set_service(name: str, on: bool) -> None:
    if name in SERVICES:
        settings_cache.set_value("Services", name, "true" if on else "false", path=DATA_SETTINGS)
