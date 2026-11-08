"""org.freedesktop.Notifications client, including action buttons (for pairing prompts)."""

import asyncio

from dbus_fast import Message, MessageType, Variant

DEST = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"


class Notifier:
    def __init__(self, bus, app_name="bluestar", icon="bluetooth-active"):
        self.bus = bus
        self.app_name = app_name
        self.icon = icon
        self._waiters = {}  # notification id -> future(action key or None)
        self._replace = {}  # key -> notification id (to update in place)

    async def start(self):
        await self.bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s",
            body=[f"type='signal',interface='{DEST}'"]))
        self.bus.add_message_handler(self._on_message)
        return self

    async def notify(self, summary, body="", actions=(), key=None, timeout_ms=-1, urgency=1):
        """Show a notification and return its id. ``actions`` is [(key, label), ...]."""
        flat = [x for pair in actions for x in pair]
        hints = {"urgency": Variant("y", urgency)}
        if actions:
            hints["resident"] = Variant("b", True)
        msg = Message(destination=DEST, path=PATH, interface=DEST, member="Notify",
                      signature="susssasa{sv}i",
                      body=[self.app_name, self._replace.get(key, 0), self.icon, summary, body,
                            flat, hints, timeout_ms])
        try:
            reply = await asyncio.wait_for(self.bus.call(msg), 5)
        except asyncio.TimeoutError:
            return 0
        if reply.message_type == MessageType.ERROR:
            return 0
        nid = reply.body[0]
        if key is not None:
            self._replace[key] = nid
        return nid

    async def ask(self, summary, body, actions, timeout=60):
        """Show a notification with buttons; return the chosen action key, or None."""
        nid = await self.notify(summary, body, actions, timeout_ms=0, urgency=2)
        if not nid:
            return None
        fut = asyncio.get_running_loop().create_future()
        self._waiters[nid] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiters.pop(nid, None)
            await self.close(nid)

    async def close(self, nid):
        if not nid:
            return
        await self.bus.call(Message(destination=DEST, path=PATH, interface=DEST,
                                    member="CloseNotification", signature="u", body=[nid]))

    def _on_message(self, msg):
        if msg.message_type != MessageType.SIGNAL or msg.interface != DEST:
            return
        if msg.member == "ActionInvoked":
            nid, action = msg.body
        elif msg.member == "NotificationClosed":
            nid, action = msg.body[0], None
        else:
            return
        fut = self._waiters.get(nid)
        if fut is not None and not fut.done():
            fut.set_result(action)
