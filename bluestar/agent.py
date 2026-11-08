"""BlueZ pairing agent (org.bluez.Agent1).

The agent only speaks D-Bus; the actual questions are asked by a *prompter*
object, so the TUI can use modal dialogs and the tray daemon notifications:

    async request_pin(name) -> str | None
    async request_passkey(name) -> int | None
    async confirm(name, passkey) -> bool
    async authorize(name) -> bool
    async authorize_service(name, service) -> bool
    display_code(name, code, entered=None)   # show a code the user types on the device
    cancel()                                  # pairing was aborted, dismiss any prompt
"""

import asyncio

from dbus_fast import DBusError
from dbus_fast.service import ServiceInterface, method

from .bluez import AGENT_MANAGER_IFACE, BluezError
from .util import device_name_from_path, profile_name

CAPABILITY = "KeyboardDisplay"
REJECTED = "org.bluez.Error.Rejected"


class _Agent1(ServiceInterface):
    def __init__(self, owner):
        super().__init__("org.bluez.Agent1")
        self.owner = owner

    def _name(self, path):
        device = self.owner.client.device(path)
        return device.alias if device else device_name_from_path(path)

    @property
    def _p(self):
        return self.owner.prompter

    @method()
    def Release(self):
        self.owner.registered = False
        self._p.cancel()

    @method()
    def Cancel(self):
        self._p.cancel()

    @method()
    async def RequestPinCode(self, device: "o") -> "s":
        pin = await self._p.request_pin(self._name(device))
        if not pin:
            raise DBusError(REJECTED, "No PIN given")
        return pin

    @method()
    async def RequestPasskey(self, device: "o") -> "u":
        passkey = await self._p.request_passkey(self._name(device))
        if passkey is None:
            raise DBusError(REJECTED, "No passkey given")
        return passkey

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        self._p.display_code(self._name(device), pincode)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        self._p.display_code(self._name(device), f"{passkey:06d}", entered)

    @method()
    async def RequestConfirmation(self, device: "o", passkey: "u"):
        if not await self._p.confirm(self._name(device), f"{passkey:06d}"):
            raise DBusError(REJECTED, "Rejected by user")

    @method()
    async def RequestAuthorization(self, device: "o"):
        if not await self._p.authorize(self._name(device)):
            raise DBusError(REJECTED, "Rejected by user")

    @method()
    async def AuthorizeService(self, device: "o", uuid: "s"):
        dev = self.owner.client.device(device)
        if dev is not None and dev.trusted:
            return
        if not await self._p.authorize_service(self._name(device), profile_name(uuid) or uuid):
            raise DBusError(REJECTED, "Rejected by user")


class Agent:
    def __init__(self, client, prompter, path):
        self.client = client
        self.prompter = prompter
        self.path = path
        self.registered = False
        self._iface = _Agent1(self)
        self._default = True
        self._exported = False
        client.listeners.append(self._on_change)

    def _on_change(self, paths):
        # "/" is marked when bluetoothd (re)appears: our registration is gone then.
        if "/" in paths and self.client.available:
            self.registered = False
            asyncio.ensure_future(self._reregister())

    async def _reregister(self):
        try:
            await self.register(self._default)
        except BluezError as err:
            print(f"bluestar: could not register pairing agent: {err.message}")

    async def register(self, default=True):
        self._default = default
        if not self._exported:
            self.client.bus.export(self.path, self._iface)
            self._exported = True
        try:
            await self.client.call("/org/bluez", AGENT_MANAGER_IFACE, "RegisterAgent", "os",
                                   [self.path, CAPABILITY])
            self.registered = True
            if default:
                await self.client.call("/org/bluez", AGENT_MANAGER_IFACE, "RequestDefaultAgent", "o",
                                       [self.path])
        except BluezError as err:
            if err.short != "AlreadyExists":
                raise
            self.registered = True

    async def unregister(self):
        if not self.registered:
            return
        self.registered = False
        try:
            await self.client.call("/org/bluez", AGENT_MANAGER_IFACE, "UnregisterAgent", "o", [self.path])
        except BluezError:
            pass
