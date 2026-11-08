"""Main window: adapter power, scanning, and the device lists."""

from gi.repository import Adw, Gio, GLib, Gtk

from .util import battery_icon_name, device_icon_name, profile_name, signal_text

SCAN_SECONDS = 60


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


class DeviceRow(Adw.ActionRow):
    def __init__(self, window, device):
        super().__init__(activatable=True, use_markup=False)
        self.window = window
        self.device = device
        self.busy = False

        self.icon = Gtk.Image(icon_size=Gtk.IconSize.LARGE)
        self.add_prefix(self.icon)

        self.battery = Gtk.Image(valign=Gtk.Align.CENTER, visible=False)
        self.add_suffix(self.battery)

        self.spinner = Adw.Spinner(visible=False, valign=Gtk.Align.CENTER)
        self.add_suffix(self.spinner)

        self.action_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.action_button.connect("clicked", lambda *_: self.primary_action())
        self.add_suffix(self.action_button)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Trusted", "dev.trust")
        section.append("Blocked", "dev.block")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Rename…", "dev.rename")
        section.append("Details", "dev.details")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Remove Device", "dev.remove")
        menu.append_section(None, section)

        self.menu_button = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                          menu_model=menu, tooltip_text="More")
        self.menu_button.add_css_class("flat")
        self.add_suffix(self.menu_button)

        group = Gio.SimpleActionGroup()
        self.trust_action = Gio.SimpleAction.new_stateful("trust", None, GLib.Variant("b", False))
        self.trust_action.connect("activate", self._on_trust)
        self.block_action = Gio.SimpleAction.new_stateful("block", None, GLib.Variant("b", False))
        self.block_action.connect("activate", self._on_block)
        for action in (self.trust_action, self.block_action):
            group.add_action(action)
        for name, cb in (("rename", self._on_rename), ("details", self._on_details), ("remove", self._on_remove)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            group.add_action(action)
        self.insert_action_group("dev", group)

        self.connect("activated", lambda *_: self.primary_action())
        self.busy_text = None
        self._changed_id = device.connect("changed", self._on_changed)
        self.refresh()

    def _on_changed(self, _device):
        self.refresh()
        self.window.on_device_changed(self.device)

    def destroy_row(self):
        if self._changed_id:
            self.device.disconnect(self._changed_id)
            self._changed_id = 0

    # -- presentation -------------------------------------------------------------

    def refresh(self):
        d = self.device
        self.set_title(d.alias)
        self.icon.set_from_icon_name(device_icon_name(d.icon))

        parts = []
        if self.busy:
            parts.append(self.busy_text)
        elif d.connected:
            parts.append("Connected")
        elif d.paired:
            parts.append("Not connected")
        elif d.rssi is not None:
            parts.append(signal_text(d.rssi))
        else:
            parts.append("Not paired")
        if d.blocked:
            parts.append("Blocked")
        if not d.has_name:
            parts.append(d.address)
        self.set_subtitle(" · ".join(p for p in parts if p))

        battery = d.battery
        self.battery.set_visible(battery is not None)
        if battery is not None:
            self.battery.set_from_icon_name(battery_icon_name(battery))
            self.battery.set_tooltip_text(f"Battery {battery}%")

        if not d.paired:
            label = "Pair"
        else:
            label = "Disconnect" if d.connected else "Connect"
        self.action_button.set_label(label)
        if d.connected:
            self.action_button.remove_css_class("suggested-action")
        else:
            self.action_button.add_css_class("suggested-action")
        self.action_button.set_sensitive(not self.busy and not d.blocked)
        self.spinner.set_visible(self.busy)

        self.trust_action.set_state(GLib.Variant("b", d.trusted))
        self.block_action.set_state(GLib.Variant("b", d.blocked))
        self.trust_action.set_enabled(d.paired)

    def set_busy(self, text=None):
        self.busy = text is not None
        self.busy_text = text
        self.refresh()
        self.window.resort()

    # -- actions --------------------------------------------------------------------

    def _done(self, what):
        def cb(err, _res):
            self.set_busy(None)
            if err:
                self.window.toast(f"{what} {self.device.alias} failed: {err.message}")
        return cb

    def primary_action(self):
        d = self.device
        if self.busy or d.blocked:
            return
        if not d.paired:
            self.pair_and_connect()
        elif d.connected:
            self.set_busy("Disconnecting…")
            d.disconnect_device(self._done("Disconnecting"))
        else:
            self.set_busy("Connecting…")
            d.connect_device(self._done("Connecting to"))

    def pair_and_connect(self):
        d = self.device
        self.window.stop_scan()  # discovery slows pairing down considerably
        self.set_busy("Pairing…")

        def paired(err, _res):
            if err:
                self.set_busy(None)
                if err.name != "org.bluez.Error.AuthenticationCanceled":
                    self.window.toast(f"Pairing with {d.alias} failed: {err.message}")
                return
            d.set_trusted(True)
            self.set_busy("Connecting…")
            d.connect_device(self._done("Connecting to"))

        d.pair(paired)

    def _on_trust(self, action, _param):
        self.device.set_trusted(not action.get_state().unpack(), self._error_toast("Changing trust"))

    def _on_block(self, action, _param):
        self.device.set_blocked(not action.get_state().unpack(), self._error_toast("Blocking"))

    def _error_toast(self, what):
        def cb(err, _res):
            if err:
                self.window.toast(f"{what} failed: {err.message}")
        return cb

    def _on_rename(self, *_args):
        entry = Gtk.Entry(text=self.device.alias, activates_default=True)
        dialog = Adw.AlertDialog(heading="Rename Device", body=f"Choose a name for {self.device.address}.")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        def on_response(_dlg, response):
            if response == "rename":
                # An empty alias makes BlueZ fall back to the device's own name.
                self.device.set_alias(entry.get_text().strip(), self._error_toast("Renaming"))

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _on_remove(self, *_args):
        d = self.device
        dialog = Adw.AlertDialog(
            heading=f"Remove {d.alias}?",
            body="The device will be disconnected and forgotten. You will need to pair it again to use it.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_dlg, response):
            if response != "remove":
                return
            adapter = d.adapter
            if adapter is None:
                return
            self.set_busy("Removing…")

            def removed(err, _res):
                if err:
                    self.set_busy(None)
                    self.window.toast(f"Removing {d.alias} failed: {err.message}")
                else:
                    self.window.toast(f"Removed {d.alias}")

            adapter.remove_device(d, removed)

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _on_details(self, *_args):
        DetailsDialog(self.device).present(self.window)


class DetailsDialog(Adw.Dialog):
    def __init__(self, device):
        super().__init__(title=device.alias, content_width=420, content_height=560)
        d = device
        page = Adw.PreferencesPage()

        info = Adw.PreferencesGroup()
        rows = [
            ("Name", d.name or "—"),
            ("Address", d.address),
            ("Type", (d.icon or "unknown").replace("-", " ").capitalize()),
            ("Paired", "Yes" if d.paired else "No"),
            ("Trusted", "Yes" if d.trusted else "No"),
            ("Connected", "Yes" if d.connected else "No"),
        ]
        if d.battery is not None:
            rows.append(("Battery", f"{d.battery}%"))
        if d.rssi is not None:
            rows.append(("Signal", f"{d.rssi} dBm ({signal_text(d.rssi).lower()})"))
        if d.device_class is not None:
            rows.append(("Class", f"0x{d.device_class:06X}"))
        if d.modalias:
            rows.append(("Modalias", d.modalias))
        for title, value in rows:
            row = Adw.ActionRow(title=title, subtitle=value, subtitle_selectable=True, use_markup=False)
            row.add_css_class("property")
            info.add(row)
        page.add(info)

        profiles = sorted({profile_name(u) for u in d.uuids} - {None})
        if profiles:
            group = Adw.PreferencesGroup(title="Profiles")
            for name in profiles:
                group.add(Adw.ActionRow(title=name, use_markup=False))
            page.add(group)

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(page)
        self.set_child(view)


class AdapterDialog(Adw.PreferencesDialog):
    def __init__(self, window):
        super().__init__(title="Adapter")
        self.window = window
        self.app = window.get_application()
        adapter = window.adapter
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()

        adapters = sorted(self.app.client.adapters.values(), key=lambda a: a.path)
        if len(adapters) > 1:
            combo = Adw.ComboRow(title="Adapter",
                                 model=Gtk.StringList.new([f"{a.alias} ({a.address})" for a in adapters]))
            combo.set_selected(adapters.index(adapter))

            def chosen(row, _pspec):
                self.app.settings["adapter"] = adapters[row.get_selected()].address
                window.set_adapter(adapters[row.get_selected()])
                self.close()

            combo.connect("notify::selected", chosen)
            group.add(combo)

        name = Adw.EntryRow(title="Name", text=adapter.alias, show_apply_button=True)
        name.connect("apply", lambda row: adapter.set_alias(row.get_text().strip(), self._err))
        group.add(name)

        visible = Adw.SwitchRow(title="Visible to Other Devices", active=adapter.discoverable)
        visible.connect("notify::active", lambda row, _p: adapter.set_discoverable(row.get_active(), self._err))
        group.add(visible)

        timeout = Adw.SpinRow.new_with_range(0, 60, 1)
        timeout.set_title("Visibility Timeout")
        timeout.set_subtitle("Minutes, 0 = stay visible")
        timeout.set_value(adapter.discoverable_timeout // 60)
        timeout.connect("notify::value",
                        lambda row, _p: adapter.set_discoverable_timeout(int(row.get_value()) * 60, self._err))
        group.add(timeout)

        pairable = Adw.SwitchRow(title="Allow Pairing", active=adapter.pairable)
        pairable.connect("notify::active", lambda row, _p: adapter.set_pairable(row.get_active(), self._err))
        group.add(pairable)

        address = Adw.ActionRow(title="Address", subtitle=adapter.address, subtitle_selectable=True)
        address.add_css_class("property")
        group.add(address)
        page.add(group)

        prefs = Adw.PreferencesGroup(title="Bluestar")
        unnamed = Adw.SwitchRow(title="Show Unnamed Devices",
                                subtitle="List nearby devices that only report an address",
                                active=self.app.settings["show-unnamed"])
        unnamed.connect("notify::active", self._on_unnamed)
        prefs.add(unnamed)
        notify = Adw.SwitchRow(title="Connection Notifications",
                               subtitle="Notify when devices connect or disconnect (tray mode)",
                               active=self.app.settings["notifications"])
        notify.connect("notify::active", lambda row, _p: self.app.settings.__setitem__("notifications", row.get_active()))
        prefs.add(notify)
        page.add(prefs)
        self.add(page)

    def _on_unnamed(self, row, _pspec):
        self.app.settings["show-unnamed"] = row.get_active()
        self.window.nearby_list.invalidate_filter()

    def _err(self, err, _res):
        if err:
            self.add_toast(Adw.Toast(title=err.message))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Bluestar", default_width=800, default_height=600,
                         icon_name="bluetooth-active-symbolic")
        self.app = app
        self.client = app.client
        self.adapter = None
        self.adapter_handler = 0
        self.rows = {}
        self.scan_timeout = 0
        self.scanning_by_us = False
        self._updating_switch = False

        # header
        header = Adw.HeaderBar()
        self.add_css_class("bluestar")
        self.title = Adw.WindowTitle(title="> bluestar_")
        self.title.add_css_class("bs-glow")
        header.set_title_widget(self.title)

        self.power_switch = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Bluetooth on/off")
        self.power_switch.connect("state-set", self._on_power_switch)
        header.pack_start(self.power_switch)

        menu = Gio.Menu()
        menu.append("Adapter Settings", "win.adapter-settings")
        menu.append("About Bluestar", "app.about")
        menu.append("Quit", "app.quit")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu")
        header.pack_end(menu_button)

        self.scan_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Search for devices (Ctrl+R)")
        self.scan_button.connect("clicked", lambda *_: self.toggle_scan())
        header.pack_end(self.scan_button)

        # pages
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)

        page = Adw.PreferencesPage()
        self.paired_group = Adw.PreferencesGroup(title="[ paired ]")
        self.paired_list = self._make_list("// no paired devices. scan to find one.")
        self.paired_group.add(self.paired_list)
        page.add(self.paired_group)

        self.nearby_group = Adw.PreferencesGroup(title="[ nearby ]")
        self.nearby_spinner = Adw.Spinner(visible=False)
        self.nearby_group.set_header_suffix(self.nearby_spinner)
        self.nearby_list = self._make_list("// press scan to look for devices.")
        self.nearby_list.set_filter_func(self._nearby_filter)
        self.nearby_group.add(self.nearby_list)
        page.add(self.nearby_group)
        self.stack.add_named(page, "devices")

        off_page = Adw.StatusPage(icon_name="bluetooth-disabled-symbolic", title="BLUETOOTH OFFLINE")
        on_button = Gtk.Button(label="[ power on ]", halign=Gtk.Align.CENTER)
        on_button.add_css_class("pill")
        on_button.add_css_class("suggested-action")
        on_button.connect("clicked", lambda *_: self.set_powered(True))
        off_page.set_child(on_button)
        self.stack.add_named(off_page, "off")

        self.stack.add_named(Adw.StatusPage(
            icon_name="bluetooth-disabled-symbolic", title="NO ADAPTER FOUND",
            description="Plug in an adapter, or check that the bluetooth service is running."), "none")

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.stack)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_content(self.toasts)

        action = Gio.SimpleAction.new("adapter-settings", None)
        action.connect("activate", lambda *_: self.adapter and AdapterDialog(self).present(self))
        self.add_action(action)
        action = Gio.SimpleAction.new("scan", None)
        action.connect("activate", lambda *_: self.toggle_scan())
        self.add_action(action)
        self.adapter_settings_action = self.lookup_action("adapter-settings")

        self.client.connect("device-added", lambda _c, d: self._add_device(d))
        self.client.connect("device-removed", lambda _c, d: self._remove_device(d))
        self.client.connect("adapter-added", lambda *_: self._pick_adapter())
        self.client.connect("adapter-removed", lambda *_: self._pick_adapter())
        self.connect("close-request", self._on_close_request)
        self.connect("notify::visible", lambda *_: None if self.get_visible() else self.stop_scan())

        self._pick_adapter()

    # -- lists -----------------------------------------------------------------------

    def _make_list(self, placeholder):
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        label = Gtk.Label(label=placeholder, wrap=True, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12, justify=Gtk.Justification.CENTER)
        label.add_css_class("dim-label")
        listbox.set_placeholder(label)
        listbox.placeholder_label = label
        listbox.set_sort_func(lambda a, b: (a.device.sort_key() > b.device.sort_key())
                              - (a.device.sort_key() < b.device.sort_key()))
        return listbox

    def _nearby_filter(self, row):
        return row.device.has_name or self.app.settings["show-unnamed"] or row.busy

    def _target_list(self, device):
        return self.paired_list if device.paired else self.nearby_list

    def _add_device(self, device):
        if self.adapter is None or device.adapter_path != self.adapter.path or device.path in self.rows:
            return
        row = DeviceRow(self, device)
        self.rows[device.path] = row
        self._target_list(device).append(row)

    def _remove_device(self, device):
        row = self.rows.pop(device.path, None)
        if row is not None:
            row.destroy_row()
            row.get_parent().remove(row)

    def on_device_changed(self, device):
        row = self.rows.get(device.path)
        if row is None:
            return
        target = self._target_list(device)
        if row.get_parent() is not target:
            row.get_parent().remove(row)
            target.append(row)
        self.resort()

    def resort(self):
        for listbox in (self.paired_list, self.nearby_list):
            listbox.invalidate_sort()
        self.nearby_list.invalidate_filter()

    # -- adapter ------------------------------------------------------------------------

    def _pick_adapter(self):
        adapters = self.client.adapters
        wanted = self.app.settings["adapter"]
        chosen = next((a for a in adapters.values() if a.address == wanted), None) or self.client.default_adapter
        if chosen is not self.adapter or (self.adapter and self.adapter.path not in adapters):
            self.set_adapter(chosen)

    def set_adapter(self, adapter):
        if self.adapter is not None and self.adapter_handler:
            self.adapter.disconnect(self.adapter_handler)
        self.stop_scan()
        for path in list(self.rows):
            self._remove_device(self.rows[path].device)
        self.adapter = adapter
        self.adapter_handler = 0
        if adapter is not None:
            self.adapter_handler = adapter.connect("changed", lambda *_: self._sync_adapter())
            for device in adapter.devices:
                self._add_device(device)
        self._sync_adapter()

    def _sync_adapter(self):
        a = self.adapter
        self.adapter_settings_action.set_enabled(a is not None)
        if a is None:
            self.stack.set_visible_child_name("none")
            self.title.set_subtitle("")
            self.power_switch.set_sensitive(False)
            self.scan_button.set_sensitive(False)
            return
        self._updating_switch = True
        self.power_switch.set_sensitive(True)
        self.power_switch.set_active(a.powered)
        self.power_switch.set_state(a.powered)
        self._updating_switch = False
        self.title.set_subtitle(a.alias + (" · visible" if a.discoverable else ""))
        self.stack.set_visible_child_name("devices" if a.powered else "off")
        self.scan_button.set_sensitive(a.powered)

        discovering = a.discovering and a.powered
        self.nearby_spinner.set_visible(discovering)
        self.nearby_list.placeholder_label.set_label(
            "scanning... put the device in pairing mode." if discovering
            else "// press scan to look for devices.")
        self.scan_button.set_icon_name("process-stop-symbolic" if discovering else "view-refresh-symbolic")
        self.scan_button.set_tooltip_text("Stop searching" if discovering else "Search for devices (Ctrl+R)")
        if not discovering:
            self._clear_scan_timer()
            self.scanning_by_us = False

    def _on_power_switch(self, _switch, state):
        if not self._updating_switch:
            self.set_powered(state)
        return True  # the real state is applied when BlueZ reports it

    def set_powered(self, value):
        if self.adapter is not None:
            self.app.set_powered(self.adapter, value, self.toast)

    # -- scanning --------------------------------------------------------------------------

    def toggle_scan(self):
        if self.adapter is None or not self.adapter.powered:
            return
        if self.adapter.discovering:
            self.stop_scan(force=True)
        else:
            self.start_scan()

    def start_scan(self):
        def started(err, _res):
            if err and err.name != "org.bluez.Error.InProgress":
                self.toast(f"Could not start searching: {err.message}")
                return
            self.scanning_by_us = True
            self._clear_scan_timer()
            self.scan_timeout = GLib.timeout_add_seconds(SCAN_SECONDS, self._scan_timed_out)

        self.adapter.start_discovery(started)

    def _scan_timed_out(self):
        self.scan_timeout = 0
        self.stop_scan()
        return GLib.SOURCE_REMOVE

    def _clear_scan_timer(self):
        if self.scan_timeout:
            GLib.source_remove(self.scan_timeout)
            self.scan_timeout = 0

    def stop_scan(self, force=False):
        self._clear_scan_timer()
        if self.adapter is not None and (self.scanning_by_us or force) and self.adapter.discovering:
            self.adapter.stop_discovery()
        self.scanning_by_us = False

    # -- misc ------------------------------------------------------------------------------

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=5))

    def _on_close_request(self, *_args):
        self.stop_scan()
        return False
