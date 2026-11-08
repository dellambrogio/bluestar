"""Asyncio client for the BlueZ D-Bus API (org.bluez on the system bus), built on dbus-fast."""

import asyncio

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

BUS_NAME = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
BATTERY_IFACE = "org.bluez.Battery1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"

DEFAULT_TIMEOUT = 25
LONG_TIMEOUT = 60      # connecting a headset can easily take 20+ seconds
PAIR_TIMEOUT = 120     # pairing waits for the user to type/confirm codes


class BluezError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.name = name or ""
        self.message = message or self.name.rsplit(".", 1)[-1]

    @property
    def short(self):
        return self.name.rsplit(".", 1)[-1]


def _unwrap(value):
    if isinstance(value, Variant):
        return _unwrap(value.value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


class _View:
    """Read-only view on one BlueZ object; always reflects the latest cached properties."""

    iface = None

    def __init__(self, client, path):
        self.client = client
        self.path = path

    def _get(self, name, default=None, iface=None):
        return self.client.objects.get(self.path, {}).get(iface or self.iface, {}).get(name, default)

    async def _set(self, name, signature, value):
        await self.client.call(self.path, PROPS_IFACE, "Set", "ssv",
                               [self.iface, name, Variant(signature, value)])

    async def _call(self, member, signature="", body=None, timeout=DEFAULT_TIMEOUT):
        return await self.client.call(self.path, self.iface, member, signature, body or [], timeout)

    def __eq__(self, other):
        return isinstance(other, _View) and other.path == self.path

    def __hash__(self):
        return hash(self.path)


class Adapter(_View):
    iface = ADAPTER_IFACE

    address = property(lambda s: s._get("Address", ""))
    name = property(lambda s: s._get("Name", ""))
    alias = property(lambda s: s._get("Alias") or s.name or s.address)
    powered = property(lambda s: s._get("Powered", False))
    power_state = property(lambda s: s._get("PowerState", "on" if s.powered else "off"))
    discoverable = property(lambda s: s._get("Discoverable", False))
    discoverable_timeout = property(lambda s: s._get("DiscoverableTimeout", 0))
    pairable = property(lambda s: s._get("Pairable", False))
    discovering = property(lambda s: s._get("Discovering", False))

    @property
    def devices(self):
        return [d for d in self.client.devices if d.adapter_path == self.path]

    async def set_powered(self, value):
        await self._set("Powered", "b", value)

    async def set_discoverable(self, value):
        await self._set("Discoverable", "b", value)

    async def set_alias(self, value):
        await self._set("Alias", "s", value)

    async def start_discovery(self):
        await self._call("StartDiscovery")

    async def stop_discovery(self):
        await self._call("StopDiscovery")

    async def remove_device(self, device):
        await self._call("RemoveDevice", "o", [device.path])


class Device(_View):
    iface = DEVICE_IFACE

    address = property(lambda s: s._get("Address", ""))
    name = property(lambda s: s._get("Name"))
    alias = property(lambda s: s._get("Alias") or s.address)
    icon = property(lambda s: s._get("Icon", ""))
    device_class = property(lambda s: s._get("Class"))
    adapter_path = property(lambda s: s._get("Adapter", ""))
    paired = property(lambda s: s._get("Paired", False))
    bonded = property(lambda s: s._get("Bonded", False))
    trusted = property(lambda s: s._get("Trusted", False))
    blocked = property(lambda s: s._get("Blocked", False))
    connected = property(lambda s: s._get("Connected", False))
    rssi = property(lambda s: s._get("RSSI"))
    uuids = property(lambda s: s._get("UUIDs", []))
    modalias = property(lambda s: s._get("Modalias"))

    @property
    def has_name(self):
        return bool(self.name)

    @property
    def battery(self):
        return self._get("Percentage", None, iface=BATTERY_IFACE)

    @property
    def adapter(self):
        return self.client.adapter(self.adapter_path)

    def sort_key(self):
        if self.paired:
            return (0, not self.connected, self.alias.casefold(), self.address)
        rssi = self.rssi if self.rssi is not None else -999
        return (1, not self.has_name, -rssi, self.alias.casefold(), self.address)

    async def connect(self):
        await self._call("Connect", timeout=LONG_TIMEOUT)

    async def disconnect(self):
        await self._call("Disconnect", timeout=LONG_TIMEOUT)

    async def pair(self):
        await self._call("Pair", timeout=PAIR_TIMEOUT)

    async def cancel_pairing(self):
        await self._call("CancelPairing")

    async def set_trusted(self, value):
        await self._set("Trusted", "b", value)

    async def set_blocked(self, value):
        await self._set("Blocked", "b", value)

    async def set_alias(self, value):
        await self._set("Alias", "s", value)


class Bluez:
    """Keeps a live mirror of every BlueZ object. ``listeners`` are called (coalesced) on change.

    Each listener gets the set of object paths that changed since the last call.
    """

    def __init__(self):
        self.bus = None
        self.objects = {}
        self.listeners = []
        self.available = False
        self._dirty = set()
        self._flush_scheduled = False

    async def start(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        for rule in (
            f"type='signal',sender='{BUS_NAME}',interface='{OM_IFACE}'",
            f"type='signal',sender='{BUS_NAME}',interface='{PROPS_IFACE}',member='PropertiesChanged'",
            f"type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{BUS_NAME}'",
        ):
            await self._dbus_call("AddMatch", "s", [rule])
        self.bus.add_message_handler(self._on_message)
        await self.reload()
        return self

    async def reload(self):
        try:
            reply = await self.call("/", OM_IFACE, "GetManagedObjects")
        except BluezError:
            self.objects = {}
            self.available = False
        else:
            self.objects = _unwrap(reply[0])
            self.available = True
        self._mark(*self.objects.keys(), "/")

    # -- queries ------------------------------------------------------------------

    @property
    def adapters(self):
        return sorted((Adapter(self, p) for p, i in self.objects.items() if ADAPTER_IFACE in i),
                      key=lambda a: a.path)

    @property
    def devices(self):
        return [Device(self, p) for p, i in self.objects.items() if DEVICE_IFACE in i]

    def adapter(self, path):
        return Adapter(self, path) if ADAPTER_IFACE in self.objects.get(path, {}) else None

    def device(self, path):
        return Device(self, path) if DEVICE_IFACE in self.objects.get(path, {}) else None

    def default_adapter(self, preferred_address=None):
        adapters = self.adapters
        for a in adapters:
            if preferred_address and a.address == preferred_address:
                return a
        powered = [a for a in adapters if a.powered]
        return (powered or adapters or [None])[0]

    # -- calls --------------------------------------------------------------------

    async def call(self, path, iface, member, signature="", body=None, timeout=DEFAULT_TIMEOUT):
        msg = Message(destination=BUS_NAME, path=path, interface=iface, member=member,
                      signature=signature, body=body or [])
        try:
            reply = await asyncio.wait_for(self.bus.call(msg), timeout)
        except asyncio.TimeoutError:
            raise BluezError("org.bluez.Error.Timeout", "Timed out") from None
        if reply.message_type == MessageType.ERROR:
            raise BluezError(reply.error_name, reply.body[0] if reply.body else "")
        return reply.body

    async def _dbus_call(self, member, signature, body):
        await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                    interface="org.freedesktop.DBus", member=member,
                                    signature=signature, body=body))

    # -- signals ------------------------------------------------------------------

    def _mark(self, *paths):
        self._dirty.update(paths)
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        asyncio.get_running_loop().call_soon(self._flush)

    def _flush(self):
        self._flush_scheduled = False
        dirty, self._dirty = self._dirty, set()
        for listener in list(self.listeners):
            try:
                listener(dirty)
            except Exception as exc:  # a broken listener must not stop the others
                print(f"bluestar: listener error: {exc!r}")

    def _on_message(self, msg):
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "NameOwnerChanged" and msg.body and msg.body[0] == BUS_NAME:
            asyncio.ensure_future(self.reload())
            return
        if msg.interface == OM_IFACE and msg.member == "InterfacesAdded":
            path, ifaces = msg.body
            self.objects.setdefault(path, {}).update(_unwrap(ifaces))
            self._mark(path)
        elif msg.interface == OM_IFACE and msg.member == "InterfacesRemoved":
            path, names = msg.body
            obj = self.objects.get(path, {})
            for name in names:
                obj.pop(name, None)
            if not obj:
                self.objects.pop(path, None)
            self._mark(path)
        elif msg.interface == PROPS_IFACE and msg.member == "PropertiesChanged":
            if not msg.path.startswith("/org/bluez"):
                return
            iface, changed, invalidated = msg.body
            props = self.objects.setdefault(msg.path, {}).setdefault(iface, {})
            props.update(_unwrap(changed))
            for name in invalidated:
                props.pop(name, None)
            self._mark(msg.path)
