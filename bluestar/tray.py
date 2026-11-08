"""System tray icon via StatusNotifierItem + com.canonical.dbusmenu, on dbus-fast.

No GTK / libappindicator needed; works with waybar, KDE, and friends.
"""

import asyncio
import os

from dbus_fast import Message, MessageType, Variant
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method, signal

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"


class MenuItem:
    def __init__(self, label=None, callback=None, *, separator=False, enabled=True, toggle=None):
        self.label = label
        self.callback = callback
        self.separator = separator
        self.enabled = enabled
        self.toggle = toggle  # None, True or False

    def props(self):
        if self.separator:
            return {"type": Variant("s", "separator")}
        props = {"label": Variant("s", self.label or ""), "enabled": Variant("b", self.enabled)}
        if self.toggle is not None:
            props["toggle-type"] = Variant("s", "checkmark")
            props["toggle-state"] = Variant("i", 1 if self.toggle else 0)
        return props


class _SNI(ServiceInterface):
    def __init__(self, tray):
        super().__init__("org.kde.StatusNotifierItem")
        self.tray = tray

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "Hardware"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "bluestar"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "bluestar"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "i":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return self.tray.icon_name

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return [self.tray.icon_name, [], "bluestar", self.tray.tooltip]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_PATH

    @method()
    def Activate(self, x: "i", y: "i"):
        self.tray._fire(self.tray.on_activate)

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        self.tray._fire(self.tray.on_secondary)

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @signal()
    def NewIcon(self):
        pass

    @signal()
    def NewToolTip(self):
        pass

    @signal()
    def NewStatus(self) -> "s":
        return "Active"


class _Menu(ServiceInterface):
    def __init__(self, tray):
        super().__init__("com.canonical.dbusmenu")
        self.tray = tray

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", property_names: "as") -> "u(ia{sv}av)":
        items = self.tray.items
        if parent_id == 0:
            children = [Variant("(ia{sv}av)", [i, item.props(), []]) for i, item in items.items()]
            layout = [0, {"children-display": Variant("s", "submenu")}, children]
        else:
            item = items.get(parent_id)
            layout = [parent_id, item.props() if item else {}, []]
        return [self.tray.revision, layout]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        items = self.tray.items
        return [[i, items[i].props()] for i in (ids or list(items)) if i in items]

    @method()
    def GetProperty(self, item_id: "i", name: "s") -> "v":
        item = self.tray.items.get(item_id)
        value = item.props().get(name) if item else None
        return value if value is not None else Variant("s", "")

    @method()
    def Event(self, item_id: "i", event_id: "s", data: "v", timestamp: "u"):
        self.tray._event(item_id, event_id)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        errors = []
        for item_id, event_id, _data, _ts in events:
            if item_id not in self.tray.items:
                errors.append(item_id)
            self.tray._event(item_id, event_id)
        return errors

    @method()
    def AboutToShow(self, item_id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [[], []]

    @signal()
    def LayoutUpdated(self) -> "ui":
        return [self.tray.revision, 0]


class TrayIcon:
    """``build_menu()`` returns a list of MenuItem; callbacks may be plain or async functions."""

    def __init__(self, bus, on_activate, build_menu, on_secondary=None):
        self.bus = bus
        self.on_activate = on_activate
        self.on_secondary = on_secondary
        self.build_menu = build_menu
        self.icon_name = "bluetooth-active-symbolic"
        self.tooltip = ""
        self.revision = 1
        self.items = {}
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._sni = _SNI(self)
        self._menu = _Menu(self)

    async def start(self):
        self._rebuild()
        self.bus.export(SNI_PATH, self._sni)
        self.bus.export(MENU_PATH, self._menu)
        await self.bus.request_name(self.name)
        await self._dbus("AddMatch", "s",
                         [f"type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{WATCHER}'"])
        self.bus.add_message_handler(self._on_message)
        await self._register()
        return self

    def update(self, icon_name, tooltip):
        if icon_name != self.icon_name:
            self.icon_name = icon_name
            self._sni.NewIcon()
        if tooltip != self.tooltip:
            self.tooltip = tooltip
            self._sni.NewToolTip()
        self._rebuild()
        self.revision += 1
        self._menu.LayoutUpdated()

    # -- internals ---------------------------------------------------------------------

    def _rebuild(self):
        self.items = dict(enumerate(self.build_menu(), start=1))

    def _event(self, item_id, event_id):
        item = self.items.get(item_id)
        if event_id == "clicked" and item and item.enabled and item.callback:
            self._fire(item.callback)

    @staticmethod
    def _fire(callback):
        if callback is None:
            return
        result = callback()
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)

    async def _dbus(self, member, signature, body):
        return await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                           interface="org.freedesktop.DBus", member=member,
                                           signature=signature, body=body))

    async def _register(self):
        reply = await self.bus.call(Message(destination=WATCHER, path="/StatusNotifierWatcher",
                                            interface=WATCHER, member="RegisterStatusNotifierItem",
                                            signature="s", body=[self.name]))
        if reply.message_type == MessageType.ERROR and "ServiceUnknown" not in (reply.error_name or ""):
            print(f"bluestar: tray registration failed: {reply.error_name}")

    def _on_message(self, msg):
        # Re-register whenever a tray host (e.g. waybar after a restart) shows up.
        if (msg.message_type == MessageType.SIGNAL and msg.member == "NameOwnerChanged"
                and msg.body and msg.body[0] == WATCHER and msg.body[2]):
            asyncio.ensure_future(self._register())
