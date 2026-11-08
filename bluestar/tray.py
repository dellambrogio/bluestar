"""System tray icon via the StatusNotifierItem + com.canonical.dbusmenu protocols.

Implemented directly on GDBus so it needs no libappindicator; works with
waybar, KDE, swaybar-alternatives, GNOME's AppIndicator extension, etc.
"""

import os

from gi.repository import Gio, GLib

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""


class MenuItem:
    def __init__(self, label=None, callback=None, *, separator=False, enabled=True,
                 toggle=None, icon=None):
        self.label = label
        self.callback = callback
        self.separator = separator
        self.enabled = enabled
        self.toggle = toggle  # None, True or False
        self.icon = icon

    def props(self):
        props = {}
        if self.separator:
            props["type"] = GLib.Variant("s", "separator")
            return props
        props["label"] = GLib.Variant("s", self.label or "")
        props["enabled"] = GLib.Variant("b", self.enabled)
        if self.toggle is not None:
            props["toggle-type"] = GLib.Variant("s", "checkmark")
            props["toggle-state"] = GLib.Variant("i", 1 if self.toggle else 0)
        if self.icon:
            props["icon-name"] = GLib.Variant("s", self.icon)
        return props


class TrayIcon:
    """``build_menu()`` returns a list of MenuItem; ``on_activate()`` handles left-click."""

    def __init__(self, on_activate, build_menu, on_secondary=None, on_scroll=None):
        self.on_activate = on_activate
        self.build_menu = build_menu
        self.on_secondary = on_secondary
        self.on_scroll = on_scroll
        self.icon_name = "bluetooth-active-symbolic"
        self.tooltip_title = "Bluestar"
        self.tooltip_body = ""
        self.revision = 1
        self.items = {}
        self.watcher_present = False
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.conn.register_object(SNI_PATH, sni_info, self._sni_call, self._sni_prop, None)
        self.conn.register_object(MENU_PATH, menu_info, self._menu_call, self._menu_prop, None)
        self._rebuild_items()

        Gio.bus_own_name_on_connection(self.conn, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None)
        Gio.bus_watch_name_on_connection(self.conn, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                         self._watcher_appeared, self._watcher_vanished)

    # -- public ---------------------------------------------------------------

    def update(self, icon_name=None, tooltip=None):
        if icon_name and icon_name != self.icon_name:
            self.icon_name = icon_name
            self._emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewIcon")
        if tooltip is not None and tooltip != self.tooltip_body:
            self.tooltip_body = tooltip
            self._emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewToolTip")
        self.menu_changed()

    def menu_changed(self):
        self._rebuild_items()
        self.revision += 1
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                   GLib.Variant("(ui)", (self.revision, 0)))

    # -- watcher ----------------------------------------------------------------

    def _watcher_appeared(self, conn, _name, _owner):
        self.watcher_present = True
        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                  lambda c, r: self._ignore(c, r))

    def _watcher_vanished(self, *_args):
        self.watcher_present = False

    @staticmethod
    def _ignore(conn, res):
        try:
            conn.call_finish(res)
        except GLib.Error as err:
            print(f"bluestar: tray registration failed: {err.message}")

    def _emit(self, path, iface, signal, params=None):
        try:
            self.conn.emit_signal(None, path, iface, signal, params)
        except GLib.Error:
            pass

    # -- StatusNotifierItem -----------------------------------------------------

    def _sni_prop(self, _conn, _sender, _path, _iface, name):
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "bluestar"),
            "Title": GLib.Variant("s", "Bluestar"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconPixmap": GLib.Variant("a(iiay)", []),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)",
                                    (self.icon_name, [], self.tooltip_title, self.tooltip_body)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def _sni_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        args = params.unpack()
        invocation.return_value(None)
        if method == "Activate":
            GLib.idle_add(self.on_activate)
        elif method == "SecondaryActivate" and self.on_secondary:
            GLib.idle_add(self.on_secondary)
        elif method == "Scroll" and self.on_scroll:
            GLib.idle_add(self.on_scroll, args[0], args[1])

    # -- dbusmenu -----------------------------------------------------------------

    def _rebuild_items(self):
        self.items = {}
        for i, item in enumerate(self.build_menu(), start=1):
            self.items[i] = item

    def _layout(self, item_id, props):
        if item_id == 0:
            children = [GLib.Variant("(ia{sv}av)", (i, item.props(), []))
                        for i, item in self.items.items()]
            root = {"children-display": GLib.Variant("s", "submenu")}
            return (0, root, children)
        item = self.items.get(item_id)
        return (item_id, item.props() if item else {}, [])

    def _menu_prop(self, _conn, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent = args[0]
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(parent, args[2]))))
        elif method == "GetGroupProperties":
            ids = args[0] or list(self.items)
            out = [(i, self.items[i].props()) for i in ids if i in self.items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            item = self.items.get(args[0])
            value = item.props().get(args[1]) if item else None
            if value is None:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "No such property")
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            invocation.return_value(None)
            self._handle_event(args[0], args[1])
        elif method == "EventGroup":
            errors = []
            for item_id, event_id, _data, _ts in args[0]:
                if item_id not in self.items and item_id != 0:
                    errors.append(item_id)
                self._handle_event(item_id, event_id)
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _handle_event(self, item_id, event_id):
        if event_id != "clicked":
            return
        item = self.items.get(item_id)
        if item and item.callback and item.enabled:
            GLib.idle_add(lambda: (item.callback(), GLib.SOURCE_REMOVE)[1])
