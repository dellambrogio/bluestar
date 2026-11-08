"""Thin GObject wrapper around the BlueZ D-Bus API (org.bluez on the system bus)."""

from gi.repository import Gio, GLib, GObject

BUS_NAME = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
BATTERY_IFACE = "org.bluez.Battery1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

# Connecting to some devices (e.g. headsets) can easily take 20+ seconds.
LONG_TIMEOUT_MS = 60_000


class BluezError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.name = name or ""
        self.message = message


def _to_error(err):
    name = Gio.DBusError.get_remote_error(err)
    Gio.DBusError.strip_remote_error(err)
    return BluezError(name, err.message)


class BluezObject(GObject.Object):
    """Base class for adapters and devices. Emits ``changed`` on any property change."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    main_iface = None

    def __init__(self, client, path):
        super().__init__()
        self.client = client
        self.path = path

    def _proxy(self, iface=None):
        return self.client.manager.get_interface(self.path, iface or self.main_iface)

    def _get(self, name, default=None, iface=None):
        proxy = self._proxy(iface)
        if proxy is None:
            return default
        value = proxy.get_cached_property(name)
        return default if value is None else value.unpack()

    def _set(self, name, variant, callback=None, iface=None):
        params = GLib.Variant("(ssv)", (iface or self.main_iface, name, variant))
        self.client.call(self.path, PROPS_IFACE, "Set", params, callback)

    def _call(self, method, params=None, callback=None, timeout=-1, iface=None):
        self.client.call(self.path, iface or self.main_iface, method, params, callback, timeout)


class Adapter(BluezObject):
    main_iface = ADAPTER_IFACE

    address = property(lambda self: self._get("Address", ""))
    name = property(lambda self: self._get("Name", ""))
    alias = property(lambda self: self._get("Alias", "") or self.name or self.address)
    powered = property(lambda self: self._get("Powered", False))
    power_state = property(lambda self: self._get("PowerState", "on" if self.powered else "off"))
    discoverable = property(lambda self: self._get("Discoverable", False))
    discoverable_timeout = property(lambda self: self._get("DiscoverableTimeout", 0))
    pairable = property(lambda self: self._get("Pairable", False))
    discovering = property(lambda self: self._get("Discovering", False))

    def set_powered(self, value, callback=None):
        self._set("Powered", GLib.Variant("b", value), callback)

    def set_discoverable(self, value, callback=None):
        self._set("Discoverable", GLib.Variant("b", value), callback)

    def set_discoverable_timeout(self, seconds, callback=None):
        self._set("DiscoverableTimeout", GLib.Variant("u", int(seconds)), callback)

    def set_pairable(self, value, callback=None):
        self._set("Pairable", GLib.Variant("b", value), callback)

    def set_alias(self, value, callback=None):
        self._set("Alias", GLib.Variant("s", value), callback)

    def start_discovery(self, callback=None):
        self._call("StartDiscovery", None, callback)

    def stop_discovery(self, callback=None):
        self._call("StopDiscovery", None, callback)

    def remove_device(self, device, callback=None):
        self._call("RemoveDevice", GLib.Variant("(o)", (device.path,)), callback)

    @property
    def devices(self):
        return [d for d in self.client.devices.values() if d.adapter_path == self.path]


class Device(BluezObject):
    main_iface = DEVICE_IFACE

    address = property(lambda self: self._get("Address", ""))
    name = property(lambda self: self._get("Name"))
    alias = property(lambda self: self._get("Alias") or self.address)
    icon = property(lambda self: self._get("Icon", ""))
    device_class = property(lambda self: self._get("Class"))
    adapter_path = property(lambda self: self._get("Adapter", ""))
    paired = property(lambda self: self._get("Paired", False))
    bonded = property(lambda self: self._get("Bonded", False))
    trusted = property(lambda self: self._get("Trusted", False))
    blocked = property(lambda self: self._get("Blocked", False))
    connected = property(lambda self: self._get("Connected", False))
    rssi = property(lambda self: self._get("RSSI"))
    uuids = property(lambda self: self._get("UUIDs", []))
    modalias = property(lambda self: self._get("Modalias"))

    @property
    def has_name(self):
        return bool(self.name)

    @property
    def battery(self):
        return self._get("Percentage", None, iface=BATTERY_IFACE)

    @property
    def adapter(self):
        return self.client.adapters.get(self.adapter_path)

    def connect_device(self, callback=None):
        self._call("Connect", None, callback, LONG_TIMEOUT_MS)

    def disconnect_device(self, callback=None):
        self._call("Disconnect", None, callback, LONG_TIMEOUT_MS)

    def pair(self, callback=None):
        # Pairing waits for the user to type/confirm codes, so be generous.
        self._call("Pair", None, callback, 120_000)

    def cancel_pairing(self, callback=None):
        self._call("CancelPairing", None, callback)

    def set_trusted(self, value, callback=None):
        self._set("Trusted", GLib.Variant("b", value), callback)

    def set_blocked(self, value, callback=None):
        self._set("Blocked", GLib.Variant("b", value), callback)

    def set_alias(self, value, callback=None):
        self._set("Alias", GLib.Variant("s", value), callback)

    def sort_key(self):
        return (not self.connected, self.alias.casefold(), self.address)


class BluezClient(GObject.Object):
    """Tracks every adapter/device BlueZ exposes and keeps them up to date."""

    __gsignals__ = {
        "adapter-added": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "adapter-removed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "device-added": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "device-removed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Emitted (coalesced) whenever anything at all changes.
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.manager = Gio.DBusObjectManagerClient.new_sync(
            self.conn, Gio.DBusObjectManagerClientFlags.NONE, BUS_NAME, "/", None, None, None
        )
        self.adapters = {}
        self.devices = {}
        self._changed_pending = False

        self.manager.connect("object-added", self._on_object_added)
        self.manager.connect("object-removed", self._on_object_removed)
        self.manager.connect("interface-added", self._on_interface_added)
        self.manager.connect("interface-removed", self._on_interface_removed)
        self.manager.connect("interface-proxy-properties-changed", self._on_props_changed)

        for obj in self.manager.get_objects():
            for iface in obj.get_interfaces():
                self._add(obj.get_object_path(), iface.get_interface_name(), emit=False)

    # -- public helpers -------------------------------------------------

    def call(self, path, iface, method, params=None, callback=None, timeout=-1):
        """Asynchronous D-Bus call; ``callback(error_or_None, result_variant)``."""

        def done(conn, res):
            try:
                result = conn.call_finish(res)
            except GLib.Error as err:
                if callback:
                    callback(_to_error(err), None)
                return
            if callback:
                callback(None, result)

        self.conn.call(BUS_NAME, path, iface, method, params, None,
                       Gio.DBusCallFlags.NONE, timeout, None, done)

    @property
    def default_adapter(self):
        if not self.adapters:
            return None
        powered = [a for a in self.adapters.values() if a.powered]
        pool = powered or list(self.adapters.values())
        return sorted(pool, key=lambda a: a.path)[0]

    @property
    def available(self):
        return self.manager.get_name_owner() is not None

    # -- internals --------------------------------------------------------

    def _queue_changed(self):
        if self._changed_pending:
            return
        self._changed_pending = True

        def fire():
            self._changed_pending = False
            self.emit("changed")
            return GLib.SOURCE_REMOVE

        GLib.idle_add(fire)

    def _add(self, path, iface_name, emit=True):
        if iface_name == ADAPTER_IFACE and path not in self.adapters:
            self.adapters[path] = Adapter(self, path)
            if emit:
                self.emit("adapter-added", self.adapters[path])
        elif iface_name == DEVICE_IFACE and path not in self.devices:
            self.devices[path] = Device(self, path)
            if emit:
                self.emit("device-added", self.devices[path])
        elif iface_name == BATTERY_IFACE and path in self.devices:
            self.devices[path].emit("changed")
        else:
            return
        self._queue_changed()

    def _on_object_added(self, _manager, obj):
        for iface in obj.get_interfaces():
            self._add(obj.get_object_path(), iface.get_interface_name())

    def _on_object_removed(self, manager, obj):
        for iface in obj.get_interfaces():
            self._on_interface_removed(manager, obj, iface)

    def _on_interface_added(self, _manager, obj, iface):
        self._add(obj.get_object_path(), iface.get_interface_name())

    def _on_interface_removed(self, _manager, obj, iface):
        path = obj.get_object_path()
        name = iface.get_interface_name()
        if name == ADAPTER_IFACE and path in self.adapters:
            self.emit("adapter-removed", self.adapters.pop(path))
        elif name == DEVICE_IFACE and path in self.devices:
            self.emit("device-removed", self.devices.pop(path))
        elif name == BATTERY_IFACE and path in self.devices:
            # The proxy is still cached during this signal; re-emit afterwards.
            GLib.idle_add(lambda: self.devices.get(path) and self.devices[path].emit("changed"))
        else:
            return
        self._queue_changed()

    def _on_props_changed(self, _manager, obj, _iface, _changed, _invalidated):
        path = obj.get_object_path()
        target = self.devices.get(path) or self.adapters.get(path)
        if target is not None:
            target.emit("changed")
            self._queue_changed()
