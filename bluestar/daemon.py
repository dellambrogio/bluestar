"""``bluestar --tray``: background daemon with tray icon, notifications and a pairing agent."""

import asyncio
import signal

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from .agent import Agent
from .bluez import Bluez, BluezError
from .notify import Notifier
from .tray import MenuItem, TrayIcon
from .util import Settings, open_tui, set_powered, toggle_tui

AGENT_PATH = "/io/github/bluestar/tray_agent"


class NotificationPrompter:
    """Pairing prompts as notifications with buttons (used while the TUI is closed)."""

    def __init__(self, notifier):
        self.n = notifier
        self._code_id = 0

    async def _yes_no(self, summary, body, yes="Accept"):
        return await self.n.ask(summary, body, [("accept", yes), ("reject", "Reject")]) == "accept"

    async def request_pin(self, name):
        # Typing a PIN needs a real input field: hand over to the TUI.
        await self.n.notify("Pairing needs a PIN", f"{name} asks for a PIN code. Pair it from bluestar.")
        open_tui()
        return None

    async def request_passkey(self, name):
        return await self.request_pin(name)

    async def confirm(self, name, passkey):
        return await self._yes_no("Confirm pairing", f"Does {name} show the code {passkey}?", "Pair")

    async def authorize(self, name):
        return await self._yes_no("Pairing request", f"{name} wants to pair with this computer.", "Allow")

    async def authorize_service(self, name, service):
        return await self._yes_no("Allow connection?", f"{name} wants to use {service}.", "Allow")

    def display_code(self, name, code, entered=None):
        asyncio.ensure_future(self.n.notify("Pairing code", f"Type {code} on {name} and press Enter.",
                                            key="pair-code", timeout_ms=0))

    def cancel(self):
        pass


class Daemon:
    def __init__(self, icon=True):
        self.icon = icon
        self.settings = Settings()
        self.client = Bluez()
        self.tray = None
        self.notifier = None
        self._connected = {}
        self.stop = None

    async def run(self):
        self.stop = asyncio.Event()
        await self.client.start()
        session = await MessageBus(bus_type=BusType.SESSION).connect()
        self.notifier = await Notifier(session).start()

        agent = Agent(self.client, NotificationPrompter(self.notifier), AGENT_PATH)
        try:
            await agent.register(default=True)
        except BluezError as err:
            print(f"bluestar: could not register pairing agent: {err.message}")

        if self.icon:
            self.tray = await TrayIcon(session, on_activate=lambda: toggle_tui(self.settings),
                                       build_menu=self._menu, on_secondary=self._toggle_power).start()
        self._connected = {d.path: d.connected for d in self.client.devices}
        self.client.listeners.append(self._on_change)
        self._on_change(set())

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        await self.stop.wait()
        await agent.unregister()

    # -- state --------------------------------------------------------------------------

    @property
    def adapter(self):
        return self.client.default_adapter(self.settings["adapter"])

    def _on_change(self, _paths):
        notify = self.settings["notifications"]
        for device in self.client.devices:
            was = self._connected.get(device.path)
            now = device.connected
            self._connected[device.path] = now
            if notify and was is not None and was != now and device.paired:
                asyncio.ensure_future(self.notifier.notify(
                    device.alias, "connected" if now else "disconnected", key=device.path, timeout_ms=4000))
        self._update_tray()

    def _update_tray(self):
        if self.tray is None:
            return
        adapter = self.adapter
        if adapter is None or not adapter.powered:
            icon, tip = "bluetooth-disabled-symbolic", "bluetooth is off" if adapter else "no adapter"
        else:
            connected = [d for d in adapter.devices if d.connected]
            icon = "bluetooth-active-symbolic"
            tip = "connected: " + ", ".join(
                d.alias + (f" ({d.battery}%)" if d.battery is not None else "") for d in connected
            ) if connected else "bluetooth is on"
        self.tray.update(icon, tip)

    def _menu(self):
        adapter = self.adapter
        items = []
        if adapter is None:
            items.append(MenuItem("No Bluetooth adapter", enabled=False))
        else:
            items.append(MenuItem("Bluetooth", self._toggle_power, toggle=adapter.powered))
            devices = sorted((d for d in adapter.devices if d.paired), key=lambda d: d.sort_key())
            if devices:
                items.append(MenuItem(separator=True))
            for d in devices:
                label = d.alias + (f"  ({d.battery}%)" if d.battery is not None else "")
                items.append(MenuItem(label, lambda d=d: self._toggle_device(d), toggle=d.connected,
                                      enabled=adapter.powered and not d.blocked))
        items.append(MenuItem(separator=True))
        items.append(MenuItem("Open bluestar", lambda: open_tui(self.settings)))
        items.append(MenuItem("Quit tray", lambda: self.stop.set()))
        return items

    # -- actions ------------------------------------------------------------------------

    async def _toggle_power(self):
        adapter = self.adapter
        if adapter is None:
            return
        try:
            await set_powered(adapter, not adapter.powered)
        except BluezError as err:
            await self.notifier.notify("Bluetooth", f"could not switch power: {err.message}")

    async def _toggle_device(self, device):
        try:
            await (device.disconnect() if device.connected else device.connect())
        except BluezError as err:
            await self.notifier.notify(device.alias, f"failed: {err.message}")


def run(icon=True):
    asyncio.run(Daemon(icon).run())
    return 0
