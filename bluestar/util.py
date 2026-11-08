"""Shared helpers: profile names, labels, settings, launching the TUI in a terminal."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys

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

TYPE_LABELS = {
    "audio-card": "speaker",
    "audio-headset": "headset",
    "audio-headphones": "headphones",
    "input-gaming": "gamepad",
    "input-keyboard": "keyboard",
    "input-mouse": "mouse",
    "input-tablet": "tablet",
    "phone": "phone",
    "computer": "computer",
    "camera-photo": "camera",
    "camera-video": "camera",
    "printer": "printer",
    "modem": "modem",
    "network-wireless": "network",
    "multimedia-player": "media",
    "video-display": "display",
}


def profile_name(uuid):
    uuid = uuid.lower()
    if uuid.endswith(_BASE_UUID_SUFFIX) and uuid.startswith("0000"):
        short = int(uuid[4:8], 16)
        return PROFILE_NAMES.get(short, f"Service 0x{short:04X}")
    return None


def type_label(icon):
    return TYPE_LABELS.get(icon, icon.split("-")[-1] if icon else "")


def signal_bars(rssi):
    """RSSI as a little 4-step meter, e.g. ▂▄▆_ ."""
    if rssi is None:
        return ""
    level = 4 if rssi >= -60 else 3 if rssi >= -70 else 2 if rssi >= -80 else 1
    return "".join(ch if i < level else "_" for i, ch in enumerate("▂▄▆█"))


def battery_bar(percent, width=5):
    if percent is None:
        return ""
    filled = max(0, min(width, round(percent / 100 * width)))
    return f"[{'|' * filled}{'.' * (width - filled)}] {percent:>3}%"


def device_name_from_path(path):
    return path.rsplit("/", 1)[-1].replace("dev_", "").replace("_", ":")


# -- settings ----------------------------------------------------------------------

def _config_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "bluestar")


class Settings:
    """Tiny JSON-backed settings store in $XDG_CONFIG_HOME/bluestar/settings.json."""

    DEFAULTS = {
        "show-unnamed": False,
        "notifications": True,
        "adapter": None,
        # Command used by the tray / --toggle to open the TUI; empty = auto-detect.
        "terminal": [],
    }

    def __init__(self):
        self.path = os.path.join(_config_dir(), "settings.json")
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


# -- rfkill -------------------------------------------------------------------------

async def set_powered(adapter, value):
    """Power an adapter on/off, unblocking rfkill first if that is what stands in the way."""
    from .bluez import BluezError

    try:
        await adapter.set_powered(value)
    except BluezError as err:
        blocked = err.short == "Blocked" or "rfkill" in err.message.lower()
        if not (value and blocked and shutil.which("rfkill")):
            raise
        proc = await asyncio.create_subprocess_exec("rfkill", "unblock", "bluetooth")
        await proc.wait()
        await asyncio.sleep(0.7)  # give the kernel a moment to bring the controller back
        await adapter.set_powered(True)


# -- TUI window management ----------------------------------------------------------------

APP_ID = "bluestar"  # window class / app_id of the terminal running the TUI


def _runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR") or "/tmp"


PIDFILE = os.path.join(_runtime_dir(), "bluestar-tui.pid")


def write_pidfile():
    try:
        with open(PIDFILE, "w") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass


def remove_pidfile():
    try:
        with open(PIDFILE) as fh:
            if fh.read().strip() == str(os.getpid()):
                os.unlink(PIDFILE)
    except OSError:
        pass


def running_tui_pid():
    try:
        with open(PIDFILE) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def terminal_command(settings):
    custom = settings["terminal"]
    if custom:
        return list(custom)
    for term, args in (
        ("kitty", ["--class", APP_ID, "--title", "bluestar", "-o", "remember_window_size=no",
                   "-o", "initial_window_width=1000", "-o", "initial_window_height=600"]),
        ("foot", ["--app-id", APP_ID, "--title", "bluestar"]),
        ("alacritty", ["--class", APP_ID, "--title", "bluestar", "-e"]),
        ("wezterm", ["start", "--class", APP_ID, "--"]),
        ("ghostty", [f"--class=io.github.{APP_ID}", "--title=bluestar", "-e"]),
    ):
        if shutil.which(term):
            return [term, *args]
    return ["x-terminal-emulator", "-e"]


def bluestar_command():
    exe = shutil.which("bluestar")
    if exe:
        return [exe]
    return [sys.executable, "-m", "bluestar"]


def toggle_tui(settings=None):
    """Close the TUI if it is running, otherwise open it in a new terminal window."""
    pid = running_tui_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            return
        except OSError:
            pass
    open_tui(settings)


def open_tui(settings=None):
    if running_tui_pid():
        return
    cmd = terminal_command(settings or Settings()) + bluestar_command()
    subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
