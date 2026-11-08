"""BlueZ pairing agent (org.bluez.Agent1) with libadwaita prompts."""

from gi.repository import Adw, Gio, GLib, Gtk

from .bluez import AGENT_MANAGER_IFACE, BUS_NAME

AGENT_PATH = "/io/github/bluestar/agent"
CAPABILITY = "KeyboardDisplay"

AGENT_XML = """
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestPinCode">
      <arg name="device" type="o" direction="in"/>
      <arg name="pincode" type="s" direction="out"/>
    </method>
    <method name="DisplayPinCode">
      <arg name="device" type="o" direction="in"/>
      <arg name="pincode" type="s" direction="in"/>
    </method>
    <method name="RequestPasskey">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="out"/>
    </method>
    <method name="DisplayPasskey">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="in"/>
      <arg name="entered" type="q" direction="in"/>
    </method>
    <method name="RequestConfirmation">
      <arg name="device" type="o" direction="in"/>
      <arg name="passkey" type="u" direction="in"/>
    </method>
    <method name="RequestAuthorization">
      <arg name="device" type="o" direction="in"/>
    </method>
    <method name="AuthorizeService">
      <arg name="device" type="o" direction="in"/>
      <arg name="uuid" type="s" direction="in"/>
    </method>
    <method name="Cancel"/>
  </interface>
</node>
"""

REJECTED = "org.bluez.Error.Rejected"
CANCELED = "org.bluez.Error.Canceled"


class Agent:
    def __init__(self, client, parent_getter):
        self.client = client
        self.parent_getter = parent_getter
        self.dialog = None
        self.invocation = None
        self.registration_id = 0
        self.registered = False

        info = Gio.DBusNodeInfo.new_for_xml(AGENT_XML)
        self.registration_id = client.conn.register_object(
            AGENT_PATH, info.interfaces[0], self._on_method_call, None, None
        )
        Gio.bus_watch_name_on_connection(
            client.conn, BUS_NAME, Gio.BusNameWatcherFlags.NONE,
            self._on_bluez_appeared, self._on_bluez_vanished,
        )

    # -- registration -----------------------------------------------------

    def _on_bluez_appeared(self, *_args):
        def registered(err, _res):
            if err:
                print(f"bluestar: could not register pairing agent: {err.message}")
                return
            self.registered = True
            self.client.call("/org/bluez", AGENT_MANAGER_IFACE, "RequestDefaultAgent",
                             GLib.Variant("(o)", (AGENT_PATH,)), None)

        self.client.call("/org/bluez", AGENT_MANAGER_IFACE, "RegisterAgent",
                         GLib.Variant("(os)", (AGENT_PATH, CAPABILITY)), registered)

    def _on_bluez_vanished(self, *_args):
        self.registered = False
        self._close_dialog()

    def unregister(self):
        if self.registered:
            self.client.call("/org/bluez", AGENT_MANAGER_IFACE, "UnregisterAgent",
                             GLib.Variant("(o)", (AGENT_PATH,)), None)
            self.registered = False

    # -- helpers ------------------------------------------------------------

    def _device_name(self, path):
        device = self.client.devices.get(path)
        return device.alias if device else path.rsplit("/", 1)[-1].replace("dev_", "").replace("_", ":")

    def _close_dialog(self):
        if self.dialog is not None:
            dialog, self.dialog = self.dialog, None
            dialog.force_close()

    def _finish_pending(self, error=CANCELED):
        if self.invocation is not None:
            inv, self.invocation = self.invocation, None
            inv.return_dbus_error(error, "Canceled")

    def _prompt(self, invocation, heading, body, accept_label="Accept",
                entry=None, on_accept=None, destructive=False, display_device=None):
        """Show an alert dialog and answer ``invocation`` with the user's choice.

        With ``display_device`` set (and no invocation) the dialog only displays a
        code; dismissing it cancels the ongoing pairing with that device.
        """
        self._close_dialog()
        self._finish_pending()

        dialog = Adw.AlertDialog(heading=heading, body=body, body_use_markup=True)
        if display_device is not None:
            dialog.add_response("close", "Cancel")
            dialog.set_close_response("close")
        else:
            dialog.add_response("reject", "Reject" if entry is None else "Cancel")
            dialog.add_response("accept", accept_label)
            dialog.set_response_appearance(
                "accept",
                Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED,
            )
            dialog.set_default_response("accept")
            dialog.set_close_response("reject")
        if entry is not None:
            dialog.set_extra_child(entry)

        dialog.bluestar_device = display_device
        self.invocation = invocation
        self.dialog = dialog

        def on_response(dlg, response):
            closed_by_us = self.dialog is not dlg
            self.dialog = None
            if display_device is not None:
                if not closed_by_us:
                    device = self.client.devices.get(display_device)
                    if device:
                        device.cancel_pairing()
                return
            if self.invocation is not invocation:
                return
            self.invocation = None
            if response == "accept":
                try:
                    result = on_accept() if on_accept else None
                except ValueError:
                    invocation.return_dbus_error(REJECTED, "Invalid input")
                    return
                invocation.return_value(result)
            else:
                invocation.return_dbus_error(REJECTED, "Rejected by user")

        dialog.connect("response", on_response)
        dialog.present(self.parent_getter())
        if entry is not None:
            entry.grab_focus()

    # -- D-Bus dispatch -----------------------------------------------------

    def _on_method_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        args = params.unpack()
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
            return
        handler(invocation, *args)

    def _m_Release(self, invocation):
        self.registered = False
        self._close_dialog()
        invocation.return_value(None)

    def _m_Cancel(self, invocation):
        self._close_dialog()
        self._finish_pending()
        invocation.return_value(None)

    def _m_RequestPinCode(self, invocation, device):
        entry = Gtk.Entry(max_length=16, placeholder_text="PIN code", activates_default=True)
        self._prompt(
            invocation, "Enter PIN Code",
            f"Type the PIN code for <b>{GLib.markup_escape_text(self._device_name(device))}</b>. "
            "It is often 0000 or 1234, or shown on the device.",
            "Pair", entry=entry,
            on_accept=lambda: GLib.Variant("(s)", (entry.get_text().strip() or _raise(),)),
        )

    def _m_RequestPasskey(self, invocation, device):
        entry = Gtk.Entry(max_length=6, placeholder_text="000000", input_purpose=Gtk.InputPurpose.DIGITS,
                          activates_default=True)
        self._prompt(
            invocation, "Enter Passkey",
            f"Type the 6-digit passkey shown on <b>{GLib.markup_escape_text(self._device_name(device))}</b>.",
            "Pair", entry=entry,
            on_accept=lambda: GLib.Variant("(u)", (int(entry.get_text().strip()),)),
        )

    def _m_DisplayPinCode(self, invocation, device, pincode):
        invocation.return_value(None)
        self._show_code(device, pincode)

    def _m_DisplayPasskey(self, invocation, device, passkey, entered):
        invocation.return_value(None)
        code = f"{passkey:06d}"
        if self.dialog is not None and self.dialog.bluestar_device == device:
            self.dialog.set_body(self._code_body(device, code, entered))
            return
        self._show_code(device, code, entered)

    def _code_body(self, device, code, entered=None):
        name = GLib.markup_escape_text(self._device_name(device))
        spaced = " ".join(code)
        if entered:
            typed = f"<b>{' '.join(code[:entered])}</b> {' '.join(code[entered:])}"
            spaced = typed
        return (f"Type this code on <b>{name}</b> and press Enter:\n\n"
                f"<span size='xx-large' font_family='monospace'>{spaced}</span>")

    def _show_code(self, device, code, entered=None):
        self._prompt(None, "Pairing Code", self._code_body(device, code, entered), display_device=device)

    def _m_RequestConfirmation(self, invocation, device, passkey):
        name = GLib.markup_escape_text(self._device_name(device))
        self._prompt(
            invocation, "Confirm Pairing",
            f"Does <b>{name}</b> show this code?\n\n"
            f"<span size='xx-large' font_family='monospace'>{' '.join(f'{passkey:06d}')}</span>",
            "Pair",
        )

    def _m_RequestAuthorization(self, invocation, device):
        name = GLib.markup_escape_text(self._device_name(device))
        self._prompt(invocation, "Pairing Request", f"<b>{name}</b> wants to pair with this computer.", "Allow")

    def _m_AuthorizeService(self, invocation, device, uuid):
        dev = self.client.devices.get(device)
        if dev is not None and dev.trusted:
            invocation.return_value(None)
            return
        from .util import profile_name

        name = GLib.markup_escape_text(self._device_name(device))
        service = GLib.markup_escape_text(profile_name(uuid) or uuid)
        self._prompt(invocation, "Allow Connection?",
                     f"<b>{name}</b> wants to use the <b>{service}</b> service.", "Allow")


def _raise():
    raise ValueError("empty")
