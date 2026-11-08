"""Application entry point: single instance, optional tray mode."""

import shutil
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .agent import Agent  # noqa: E402
from .bluez import BluezClient  # noqa: E402
from .util import Settings  # noqa: E402


class BluestarApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.client = None
        self.agent = None
        self.tray = None
        self.window = None
        self.settings = Settings()
        self._connected = {}

        self.add_main_option("tray", ord("t"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Run in the background with a tray icon", None)
        self.add_main_option("toggle", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Show the window, or hide it if it is already visible", None)
        self.add_main_option("version", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Print the version and exit", None)

    # -- lifecycle ----------------------------------------------------------------------

    def do_startup(self):
        Adw.Application.do_startup(self)
        self._load_theme()
        try:
            self.client = BluezClient()
        except GLib.Error as err:
            print(f"bluestar: cannot reach the system bus: {err.message}", file=sys.stderr)
            raise SystemExit(1)

        self.agent = Agent(self.client, lambda: self.get_active_window())
        self.client.connect("changed", lambda *_: self._on_client_changed())
        self._connected = {p: d.connected for p, d in self.client.devices.items()}

        for name, cb, accels in (
            ("quit", lambda *_: self.quit(), ["<primary>q"]),
            ("about", lambda *_: self._show_about(), []),
            ("show", lambda *_: self.show_window(), []),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("win.scan", ["<primary>r", "F5"])
        self.set_accels_for_action("window.close", ["<primary>w"])

    def _load_theme(self):
        import os

        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        provider = Gtk.CssProvider()
        provider.load_from_path(os.path.join(os.path.dirname(__file__), "style.css"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    def do_shutdown(self):
        if self.window is not None:
            self.window.stop_scan()
        if self.agent is not None:
            self.agent.unregister()
            # Flush the unregister call before the process goes away.
            self.client.conn.flush_sync(None)
        Adw.Application.do_shutdown(self)

    def do_command_line(self, command_line):
        options = command_line.get_options_dict().end().unpack()
        if options.get("version"):
            command_line.print_literal(f"bluestar {__version__}\n")
            return 0
        if options.get("tray"):
            self.start_tray()
        elif options.get("toggle"):
            self.toggle_window()
        else:
            self.show_window()
        return 0

    # -- window -------------------------------------------------------------------------

    def show_window(self):
        if self.window is None:
            from .window import MainWindow

            self.window = MainWindow(self)
            self.window.set_hide_on_close(self.tray is not None)
            self.window.connect("destroy", self._on_window_destroyed)
        self.window.present()
        return GLib.SOURCE_REMOVE

    def toggle_window(self):
        if self.window is not None and self.window.get_visible():
            self.window.set_visible(False)
        else:
            self.show_window()
        return GLib.SOURCE_REMOVE

    def _on_window_destroyed(self, *_args):
        self.window = None

    # -- tray ---------------------------------------------------------------------------

    def start_tray(self):
        if self.tray is not None:
            return
        from .tray import TrayIcon

        self.tray = TrayIcon(on_activate=self.toggle_window, build_menu=self._tray_menu,
                             on_secondary=self._toggle_power_default)
        self.hold()
        if self.window is not None:
            self.window.set_hide_on_close(True)
        self._update_tray()

    def _tray_menu(self):
        from .tray import MenuItem

        adapter = self.client.default_adapter if self.client else None
        items = []
        if adapter is None:
            items.append(MenuItem("No Bluetooth adapter", enabled=False))
        else:
            items.append(MenuItem("Bluetooth", lambda: self._toggle_power_default(), toggle=adapter.powered))
            devices = sorted((d for d in adapter.devices if d.paired), key=lambda d: d.sort_key())
            if devices:
                items.append(MenuItem(separator=True))
            for device in devices:
                label = device.alias
                if device.battery is not None:
                    label += f"  ({device.battery}%)"
                items.append(MenuItem(label, lambda d=device: self._toggle_device(d),
                                      toggle=device.connected, enabled=adapter.powered and not device.blocked))
        items.append(MenuItem(separator=True))
        items.append(MenuItem("Open Bluestar…", self.show_window))
        items.append(MenuItem("Quit", self.quit))
        return items

    def _update_tray(self):
        if self.tray is None:
            return
        adapter = self.client.default_adapter
        if adapter is None or not adapter.powered:
            icon, tip = "bluetooth-disabled-symbolic", "Bluetooth is off" if adapter else "No adapter"
        else:
            connected = [d for d in adapter.devices if d.connected]
            icon = "bluetooth-active-symbolic"
            if connected:
                tip = "Connected: " + ", ".join(
                    d.alias + (f" ({d.battery}%)" if d.battery is not None else "") for d in connected)
            else:
                tip = "Bluetooth is on"
        self.tray.update(icon, tip)

    def _toggle_power_default(self):
        adapter = self.client.default_adapter
        if adapter is not None:
            self.set_powered(adapter, not adapter.powered)
        return GLib.SOURCE_REMOVE

    def _toggle_device(self, device):
        cb = self._notify_error(f"Could not {'disconnect' if device.connected else 'connect'} {device.alias}")
        if device.connected:
            device.disconnect_device(cb)
        else:
            device.connect_device(cb)

    # -- shared operations ----------------------------------------------------------------

    def set_powered(self, adapter, value, on_error=None):
        """Power an adapter on/off, unblocking rfkill if that is what stands in the way."""

        def report(message):
            if on_error:
                on_error(message)
            else:
                self._notify("Bluetooth", message)

        def done(err, _res):
            if err is None:
                return
            blocked = err.name == "org.bluez.Error.Blocked" or "rfkill" in err.message.lower()
            if value and blocked and shutil.which("rfkill"):
                try:
                    subprocess.run(["rfkill", "unblock", "bluetooth"], check=True, timeout=5)
                except (subprocess.SubprocessError, OSError) as exc:
                    report(f"Bluetooth is blocked and could not be unblocked: {exc}")
                    return
                # Give the kernel a moment to bring the controller back.
                GLib.timeout_add(700, lambda: (adapter.set_powered(True, lambda e, _r: e and report(
                    f"Could not turn Bluetooth on: {e.message}")), GLib.SOURCE_REMOVE)[1])
                return
            report(f"Could not turn Bluetooth {'on' if value else 'off'}: {err.message}")

        adapter.set_powered(value, done)

    def _notify_error(self, prefix):
        def cb(err, _res):
            if err:
                if self.window is not None and self.window.get_visible():
                    self.window.toast(f"{prefix}: {err.message}")
                else:
                    self._notify("Bluetooth", f"{prefix}: {err.message}")
        return cb

    def _notify(self, title, body, key=None):
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(Gio.ThemedIcon.new("bluetooth-active-symbolic"))
        self.send_notification(key, notification)

    def _on_client_changed(self):
        # Connection notifications are only useful when running in the background.
        for path, device in self.client.devices.items():
            was = self._connected.get(path)
            now = device.connected
            self._connected[path] = now
            if (self.tray is not None and self.settings["notifications"] and was is not None
                    and was != now and device.paired):
                self._notify(device.alias, "Connected" if now else "Disconnected", key=f"dev{hash(path)}")
        for path in list(self._connected):
            if path not in self.client.devices:
                del self._connected[path]
        self._update_tray()

    def _show_about(self):
        about = Adw.AboutDialog(
            application_name="Bluestar",
            application_icon="bluetooth-active-symbolic",
            version=__version__,
            comments="A simple Bluetooth manager",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self.get_active_window())


def main(argv=None):
    app = BluestarApp()
    return app.run(argv if argv is not None else sys.argv)
