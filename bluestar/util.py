"""Small helpers: icons, profile names, persistent settings."""

import json
import os

from gi.repository import Gdk, GLib, Gtk

_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"

PROFILE_NAMES = {
    0x1101: "Serial Port",
    0x1103: "Dial-up Networking",
    0x1105: "OBEX Object Push",
    0x1106: "OBEX File Transfer",
    0x1108: "Headset",
    0x110A: "Audio Source",
    0x110B: "Audio Sink",
    0x110C: "Remote Control Target",
    0x110D: "Advanced Audio Distribution",
    0x110E: "Remote Control",
    0x110F: "Remote Control Controller",
    0x1112: "Headset Audio Gateway",
    0x1115: "Personal Area Network (PANU)",
    0x1116: "Network Access Point",
    0x1117: "Group Network",
    0x111E: "Handsfree",
    0x111F: "Handsfree Audio Gateway",
    0x1124: "Human Interface Device",
    0x112D: "SIM Access",
    0x112F: "Phonebook Access",
    0x1132: "Message Access",
    0x1133: "Message Notification",
    0x1200: "PnP Information",
    0x1203: "Generic Audio",
    0x1800: "Generic Access",
    0x1801: "Generic Attribute",
    0x180A: "Device Information",
    0x180F: "Battery Service",
    0x1812: "HID over GATT",
    0x184E: "Audio Stream Control",
    0x184F: "Broadcast Audio Scan",
    0x1850: "Published Audio Capabilities",
    0x1853: "Common Audio",
    0xFE2C: "Google Fast Pair",
}

ICON_FALLBACKS = {
    "audio-card": ["audio-speakers", "audio-card"],
    "audio-headset": ["audio-headset", "audio-headphones"],
    "audio-headphones": ["audio-headphones", "audio-headset"],
    "input-gaming": ["input-gaming"],
    "input-keyboard": ["input-keyboard"],
    "input-mouse": ["input-mouse"],
    "input-tablet": ["input-tablet"],
    "phone": ["phone", "smartphone"],
    "computer": ["computer"],
    "camera-photo": ["camera-photo"],
    "camera-video": ["camera-video"],
    "printer": ["printer"],
    "modem": ["network-wireless"],
    "network-wireless": ["network-wireless"],
    "multimedia-player": ["multimedia-player"],
    "video-display": ["video-display"],
}


def profile_name(uuid):
    uuid = uuid.lower()
    if uuid.endswith(_BASE_UUID_SUFFIX) and uuid.startswith("0000"):
        short = int(uuid[4:8], 16)
        return PROFILE_NAMES.get(short, f"Service 0x{short:04X}")
    return None


def device_icon_name(icon):
    display = Gdk.Display.get_default()
    theme = Gtk.IconTheme.get_for_display(display) if display else None
    candidates = ICON_FALLBACKS.get(icon, [icon] if icon else [])
    for name in candidates:
        if theme is None or theme.has_icon(f"{name}-symbolic"):
            return f"{name}-symbolic"
    return "bluetooth-symbolic"


def battery_icon_name(percent):
    level = min(100, max(0, int(round(percent / 10.0)) * 10))
    return f"battery-level-{level}-symbolic"


def signal_text(rssi):
    if rssi is None:
        return None
    if rssi >= -60:
        return "Excellent signal"
    if rssi >= -70:
        return "Good signal"
    if rssi >= -80:
        return "Fair signal"
    return "Weak signal"


class Settings:
    """Tiny JSON-backed settings store in $XDG_CONFIG_HOME/bluestar/settings.json."""

    DEFAULTS = {
        "show-unnamed": False,
        "notifications": True,
        "adapter": None,
    }

    def __init__(self):
        self.path = os.path.join(GLib.get_user_config_dir(), "bluestar", "settings.json")
        self.data = dict(self.DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass
