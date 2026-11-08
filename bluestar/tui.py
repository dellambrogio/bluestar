"""The bluestar terminal UI (Textual)."""

import asyncio
import signal
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Input, Static

from . import __version__
from .agent import Agent
from .bluez import Bluez, BluezError
from .util import (Settings, battery_bar, place_on_sway, profile_name, remove_pidfile, set_powered, signal_bars,
                   type_label, write_pidfile)

AGENT_PATH = "/io/github/bluestar/tui_agent"
SCAN_SECONDS = 60
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

GREEN = "#00ff00"
GREEN_DIM = "#00b300"
GREEN_DARK = "#004d00"
YELLOW = "#ffff00"
RED = "#ff3333"

THEME = Theme(
    name="bluestar",
    primary=GREEN,
    secondary=GREEN_DIM,
    accent=GREEN,
    foreground=GREEN,
    background="#000000",
    surface="#000000",
    panel="#001a00",
    boost="#001a00",
    success=GREEN,
    warning=YELLOW,
    error=RED,
    dark=True,
    variables={
        "block-cursor-background": GREEN,
        "block-cursor-foreground": "#000000",
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": GREEN_DARK,
        "block-cursor-blurred-foreground": GREEN,
        "footer-background": "#000000",
        "footer-key-foreground": "#000000",
        "footer-key-background": GREEN,
        "footer-description-foreground": GREEN,
        "input-cursor-background": GREEN,
        "input-cursor-foreground": "#000000",
        "input-selection-background": GREEN_DARK,
        "scrollbar": GREEN_DARK,
        "scrollbar-hover": GREEN_DIM,
        "scrollbar-active": GREEN,
        "scrollbar-background": "#000000",
        "border": GREEN,
        "border-blurred": GREEN_DARK,
    },
)

CSS = f"""
Screen {{
    background: #000000;
    color: {GREEN};
    overflow: hidden;
}}
#body {{
    height: 1fr;
    overflow: hidden;
}}
#header {{
    height: 1;
    padding: 0 1;
    background: #001a00;
}}
DeviceTable {{
    background: #000000;
    border: round {GREEN_DARK};
    border-title-color: {GREEN_DIM};
    border-title-style: bold;
    width: 1fr;
    padding: 0 1;
    scrollbar-size-vertical: 1;
    overflow-x: hidden;
}}
DeviceTable:focus {{
    border: round {GREEN};
    border-title-color: {GREEN};
}}
DeviceTable > .datatable--header {{
    background: #000000;
    color: {GREEN_DIM};
    text-style: bold;
}}
DeviceTable > .datatable--hover {{
    background: #001a00;
}}
#paired, #nearby {{
    height: auto;
}}
#spacer {{
    height: 1fr;
}}
#hint {{
    height: 1;
    text-wrap: nowrap;
    text-overflow: ellipsis;
    padding: 0 1;
    color: {GREEN_DIM};
    display: none;
}}
#log {{
    height: 1;
    padding: 0 1;
    color: {GREEN_DIM};
}}
Footer {{
    background: #000000;
}}

ModalScreen {{
    align: center middle;
    background: #000000 70%;
}}
.dialog {{
    width: 64;
    max-width: 100%;
    height: auto;
    max-height: 100%;
    overflow-y: auto;
    border: double {GREEN};
    border-title-color: {GREEN};
    border-title-style: bold;
    background: #000000;
    padding: 1 2;
}}
.dialog .body {{
    margin-bottom: 1;
}}
.dialog .code {{
    text-align: center;
    text-style: bold;
    color: #000000;
    background: {GREEN};
    margin-bottom: 1;
}}
.dialog .keys {{
    color: {GREEN_DIM};
}}
.dialog Input {{
    background: #000000;
    border: tall {GREEN_DIM};
    margin-bottom: 1;
}}
.dialog Input:focus {{
    border: tall {GREEN};
}}
.danger {{
    border: double {RED};
    border-title-color: {RED};
}}
"""


# -- modal dialogs ------------------------------------------------------------------------

class ConfirmScreen(ModalScreen):
    BINDINGS = [
        Binding("y,enter", "answer(True)", "yes", show=False),
        Binding("n,escape", "answer(False)", "no", show=False),
    ]

    def __init__(self, title, body, yes="yes", no="no", danger=False, code=None):
        super().__init__()
        self.title_text, self.body, self.yes, self.no = title, body, yes, no
        self.danger, self.code = danger, code

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog danger" if self.danger else "dialog") as box:
            box.border_title = f" {self.title_text} "
            yield Static(self.body, classes="body")
            if self.code:
                yield Static(" ".join(self.code), classes="code")
            yield Static(f"\\[y] {self.yes}    \\[n] {self.no}", classes="keys")

    def action_answer(self, value):
        self.dismiss(value)


class InputScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, title, body, value="", placeholder="", digits=False, max_length=0):
        super().__init__()
        self.title_text, self.body = title, body
        self.value, self.placeholder, self.digits, self.max_length = value, placeholder, digits, max_length

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as box:
            box.border_title = f" {self.title_text} "
            yield Static(self.body, classes="body")
            yield Input(value=self.value, placeholder=self.placeholder, max_length=self.max_length,
                        restrict=r"[0-9]*" if self.digits else None)
            yield Static("\\[enter] ok    \\[esc] cancel", classes="keys")

    def on_input_submitted(self, event):
        self.dismiss(event.value)

    def action_cancel(self):
        self.dismiss(None)


class CodeScreen(ModalScreen):
    """Shows a code to type on the device; closed by BlueZ (paired/cancelled) or by Esc."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, name, code, entered=None):
        super().__init__()
        self.device_name, self.code, self.entered = name, code, entered

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as box:
            box.border_title = " pairing code "
            yield Static(f"type this code on [b]{self.device_name}[/b] and press enter:", classes="body")
            yield Static(self._code_text(), classes="code", id="code")
            yield Static("\\[esc] cancel pairing", classes="keys")

    def _code_text(self):
        return " ".join(self.code)

    def update_entered(self, entered):
        typed = " ".join(self.code[:entered])
        rest = " ".join(self.code[entered:])
        self.query_one("#code", Static).update(f"[u]{typed}[/u] {rest}" if entered else self._code_text())

    def action_cancel(self):
        self.dismiss(False)


class InfoScreen(ModalScreen):
    BINDINGS = [Binding("escape,i,q,enter", "close", "close", show=False)]

    def __init__(self, device):
        super().__init__()
        self.device = device

    def compose(self) -> ComposeResult:
        d = self.device
        rows = [
            ("name", d.name or "-"),
            ("alias", d.alias),
            ("address", d.address),
            ("type", type_label(d.icon) or "-"),
            ("paired", "yes" if d.paired else "no"),
            ("trusted", "yes" if d.trusted else "no"),
            ("blocked", "yes" if d.blocked else "no"),
            ("connected", "yes" if d.connected else "no"),
        ]
        if d.battery is not None:
            rows.append(("battery", f"{d.battery}%"))
        if d.rssi is not None:
            rows.append(("rssi", f"{d.rssi} dBm"))
        if d.device_class is not None:
            rows.append(("class", f"0x{d.device_class:06X}"))
        if d.modalias:
            rows.append(("modalias", d.modalias))
        profiles = sorted({profile_name(u) for u in d.uuids} - {None})
        text = Text()
        for key, value in rows:
            text.append(f"{key:>10}  ", style=GREEN_DIM)
            text.append(f"{value}\n")
        if profiles:
            text.append("\n  profiles\n", style=f"bold {GREEN_DIM}")
            for p in profiles:
                text.append(f"    - {p}\n")
        with Vertical(classes="dialog") as box:
            box.border_title = f" {d.alias} "
            yield Static(text, classes="body")
            yield Static("\\[esc] close", classes="keys")

    def action_close(self):
        self.dismiss(None)


# -- device table -----------------------------------------------------------------------------

class DeviceTable(DataTable):
    """DataTable that hands focus to the neighbouring table at its top/bottom edge."""

    BINDINGS = [Binding("enter", "select_cursor", "connect", show=True)]

    def action_cursor_down(self):
        if self.row_count == 0 or self.cursor_row >= self.row_count - 1:
            self.app.focus_table(self, +1)
        else:
            super().action_cursor_down()

    def action_cursor_up(self):
        if self.row_count == 0 or self.cursor_row <= 0:
            self.app.focus_table(self, -1)
        else:
            super().action_cursor_up()


# (key, label, width) in display order; the name column is flexible (width = minimum).
COLUMNS = [("glyph", "", 1), ("name", "name", 12), ("type", "type", 10), ("battery", "battery", 12),
           ("signal", "signal", 6), ("state", "state", 16), ("address", "address", 17)]
# Columns dropped first when the terminal gets narrow.
# The state column goes last: the glyph in front of the name still shows ●/○/+.
DROP_ORDER = ["address", "type", "signal", "battery", "state"]
NAME_MAX = 32
CELL_PAD = 2      # DataTable puts one space on each side of every cell
TABLE_CHROME = 5  # border (2) + padding (2) + vertical scrollbar (1)


def column_layout(width):
    """Pick the columns that fit in ``width`` cells: [(key, label, width), ...]."""
    avail = max(0, width - TABLE_CHROME)
    keys = [k for k, _l, _w in COLUMNS]
    widths = {k: w for k, _l, w in COLUMNS}

    def need(ks):
        return sum(widths[k] + CELL_PAD for k in ks)

    for drop in DROP_ORDER:
        if need(keys) <= avail:
            break
        keys.remove(drop)
    if "state" in keys:  # "paired · trusted" needs 16, but a longer name matters more
        short = max(0, need(keys) + 4 - avail)
        widths["state"] -= min(short, 6)
    spare = max(0, avail - need(keys))
    widths["name"] += min(spare, NAME_MAX - widths["name"])
    if need(keys) > avail:  # tiny terminal: squeeze the name
        widths["name"] = max(4, widths["name"] - (need(keys) - avail))
    return [(k, label, widths[k]) for k, label, _w in COLUMNS if k in keys]


# -- the app ----------------------------------------------------------------------------------

class TuiPrompter:
    """Pairing-agent prompts as modal screens."""

    def __init__(self, app):
        self.app = app
        self.screen = None

    async def _show(self, screen):
        self.cancel()
        fut = asyncio.get_running_loop().create_future()
        self.screen = screen

        def done(result):
            if self.screen is screen:
                self.screen = None
            if not fut.done():
                fut.set_result(result)

        self.app.push_screen(screen, done)
        return await fut

    async def request_pin(self, name):
        return await self._show(InputScreen("pin code", f"enter the PIN for [b]{name}[/b] (often 0000 or 1234)",
                                            max_length=16)) or None

    async def request_passkey(self, name):
        value = await self._show(InputScreen("passkey", f"enter the 6-digit passkey shown on [b]{name}[/b]",
                                             digits=True, max_length=6))
        return int(value) if value else None

    async def confirm(self, name, passkey):
        return bool(await self._show(ConfirmScreen("confirm pairing", f"does [b]{name}[/b] show this code?",
                                                   "pair", "reject", code=passkey)))

    async def authorize(self, name):
        return bool(await self._show(ConfirmScreen("pairing request",
                                                   f"[b]{name}[/b] wants to pair with this computer.",
                                                   "allow", "reject")))

    async def authorize_service(self, name, service):
        return bool(await self._show(ConfirmScreen("allow connection?",
                                                   f"[b]{name}[/b] wants to use [b]{service}[/b].",
                                                   "allow", "reject")))

    def display_code(self, name, code, entered=None):
        if isinstance(self.screen, CodeScreen) and self.screen.device_name == name:
            self.screen.update_entered(entered or 0)
            return
        self.cancel()
        screen = CodeScreen(name, code, entered)
        self.screen = screen

        def done(result):
            if self.screen is screen:
                self.screen = None
            if result is False:  # user pressed Esc: abort pairing
                self.app.cancel_pairing()

        self.app.push_screen(screen, done)

    def cancel(self):
        screen, self.screen = self.screen, None
        if screen is not None and screen.is_attached and self.app.screen is screen:
            screen.dismiss(None)


class BluestarTUI(App):
    TITLE = "bluestar"
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("s", "scan", "scan"),
        Binding("c", "primary", "connect/pair", show=False),
        Binding("x", "disconnect", "disconnect"),
        Binding("d,delete", "remove", "remove"),
        Binding("t", "trust", "trust"),
        Binding("b", "block", "block", show=False),
        Binding("r", "rename", "rename"),
        Binding("i", "info", "info"),
        Binding("p", "power", "power"),
        Binding("v", "visible", "visible", show=False),
        Binding("u", "unnamed", "unnamed", show=False),
        Binding("q,escape", "quit", "quit"),
    ]

    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.client = Bluez()
        self.agent = None
        self.prompter = TuiPrompter(self)
        self.busy = {}          # device path -> "connecting..." etc.
        self.scan_started = None
        self.scan_task = None
        self.frame = 0
        self.message = Text("")
        self.columns = column_layout(120)

    # -- layout ---------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Vertical(id="body"):
            yield from self._compose_body()
        yield Static(id="log")
        yield Footer()

    def _compose_body(self):
        paired = DeviceTable(id="paired", cursor_type="row", zebra_stripes=False)
        paired.border_title = "\\[ paired ]"
        nearby = DeviceTable(id="nearby", cursor_type="row", zebra_stripes=False)
        nearby.border_title = "\\[ nearby ]"
        for table in (paired, nearby):
            self._add_columns(table)
            yield table
        yield Static(id="hint")
        yield Static(id="spacer")

    async def on_mount(self):
        self.register_theme(THEME)
        self.theme = "bluestar"
        write_pidfile()
        self.run_worker(place_on_sway(self.settings["position"]), exclusive=False)
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: self.call_later(self.action_quit))
        try:
            await self.client.start()
        except Exception as exc:  # no system bus, etc.
            self.log_message(f"cannot reach bluez: {exc}", error=True)
            return
        self.client.listeners.append(lambda _paths: self.refresh_all())
        self.agent = Agent(self.client, self.prompter, AGENT_PATH)
        try:
            await self.agent.register(default=True)
        except BluezError as err:
            self.log_message(f"pairing agent unavailable: {err.message}", error=True)
        self.set_interval(0.1, self._tick)
        self.refresh_all()
        self.main.query_one("#paired", DeviceTable).focus()
        self.log_message("ready. [s] scan  [enter] connect/disconnect  [x] disconnect  [d] remove")

    async def action_quit(self):
        await self._stop_scan()
        if self.agent is not None:
            await self.agent.unregister()
        remove_pidfile()
        self.exit()

    def on_unmount(self):
        remove_pidfile()

    @property
    def main(self):
        """The device screen (queries on ``self`` would hit a modal dialog when one is open)."""
        return self.screen_stack[0]

    # -- rendering ----------------------------------------------------------------------------

    @property
    def adapter(self):
        return self.client.default_adapter(self.settings["adapter"])

    def _tick(self):
        self.frame = (self.frame + 1) % len(SPINNER)
        a = self.adapter
        if a is not None and a.discovering and self.scan_started and \
                time.monotonic() - self.scan_started > SCAN_SECONDS:
            self.run_worker(self._stop_scan(), exclusive=False)
        if self.busy or (a is not None and a.discovering):
            self._render_header()
            if self.busy:
                self.refresh_all()

    def _render_header(self):
        a = self.adapter
        width = self.size.width - 2
        wide, narrow = width >= 90, width < 48
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("> bluestar_" if not narrow else ">_", style=f"bold {GREEN}")
        if wide:
            t.append(f"  v{__version__}", style=GREEN_DARK)
        t.append("  ")
        if a is None:
            t.append("no adapter" if self.client.available else "bluetoothd not running", style=f"bold {RED}")
        else:
            if not narrow:
                t.append(a.alias + "  ", style=f"bold {GREEN}")
            t.append("power:" if not narrow else "pwr:", style=GREEN_DIM)
            t.append(" on " if a.powered else " off ",
                     style=f"bold #000000 on {GREEN}" if a.powered else f"bold #000000 on {RED}")
            if not narrow or a.discoverable:
                t.append("  visible:" if not narrow else " vis:", style=GREEN_DIM)
                t.append("yes" if a.discoverable else "no", style=GREEN if a.discoverable else GREEN_DIM)
            t.append("  scan:" if not narrow else " ", style=GREEN_DIM)
            if self.scan_started and a.discovering:
                left = SCAN_SECONDS - int(time.monotonic() - self.scan_started)
                t.append(f"{SPINNER[self.frame]} {'scanning ' if not narrow else ''}{max(left, 0)}s",
                         style=f"bold {GREEN}")
            elif a.discovering:
                t.append(f"{SPINNER[self.frame]} " + ("scanning (other app)" if wide else "other app"),
                         style=GREEN_DIM)
            elif not narrow:
                t.append("idle", style=GREEN_DIM)
        self.main.query_one("#header", Static).update(t)

    def _add_columns(self, table):
        for key, label, width in self.columns:
            table.add_column(label, width=width, key=key)

    def on_resize(self, event):
        # Split the body between the two lists, but always keep at least one row of each visible.
        body = max(0, event.size.height - 3)  # header, log, footer
        header = event.size.height >= 16
        floor = 4 if header else 3
        paired, nearby = self._tables()
        paired.styles.max_height = max(floor, body * 40 // 100)
        nearby.styles.max_height = max(floor, body * 55 // 100)
        for table in (paired, nearby):
            table.show_header = header
        layout = column_layout(event.size.width)
        if layout == self.columns:
            self._render_header()
            return
        self.columns = layout
        for table in self._tables():
            selected = self._selected_key(table)
            table.clear(columns=True)
            self._add_columns(table)
            table.refresh()
            table._bluestar_selected = selected
        self.refresh_all()

    def _tables(self):
        return [self.main.query_one("#paired", DeviceTable), self.main.query_one("#nearby", DeviceTable)]

    def _row(self, d):
        busy = self.busy.get(d.path)
        if busy:
            glyph, gstyle = SPINNER[self.frame], f"bold {YELLOW}"
        elif d.blocked:
            glyph, gstyle = "x", f"bold {RED}"
        elif d.connected:
            glyph, gstyle = "●", f"bold {GREEN}"
        elif d.paired:
            glyph, gstyle = "○", GREEN_DIM
        else:
            glyph, gstyle = "+", GREEN_DIM

        if busy:
            state = Text(busy, style=YELLOW)
        elif d.blocked:
            state = Text("blocked", style=RED)
        elif d.connected:
            state = Text("connected", style=f"bold {GREEN}")
        elif d.paired:
            state = Text("paired" + (" · trusted" if d.trusted else ""), style=GREEN_DIM)
        else:
            state = Text("new", style=GREEN_DIM)

        name_style = f"bold {GREEN}" if d.connected else (GREEN if d.has_name else GREEN_DIM)
        state.overflow, state.no_wrap = "ellipsis", True
        cells = {
            "glyph": Text(glyph, style=gstyle),
            "name": Text(d.alias, style=name_style, overflow="ellipsis", no_wrap=True),
            "type": Text(type_label(d.icon), style=GREEN_DIM, overflow="ellipsis", no_wrap=True),
            "battery": Text(battery_bar(d.battery)),
            "signal": self._signal(d.rssi),
            "state": state,
            "address": Text(d.address, style=GREEN_DARK),
        }
        return [cells[key] for key, _l, _w in self.columns]

    @staticmethod
    def _signal(rssi):
        t = Text()
        if rssi is None:
            return t
        bars = signal_bars(rssi)
        for i, ch in enumerate("▂▄▆█"):
            t.append(ch, style=GREEN if bars[i] != "_" else GREEN_DARK)
        return t

    def refresh_all(self):
        self._render_header()
        a = self.adapter
        devices = sorted(a.devices, key=lambda d: d.sort_key()) if a else []
        show_unnamed = self.settings["show-unnamed"]
        paired = [d for d in devices if d.paired]
        nearby = [d for d in devices if not d.paired and (d.has_name or show_unnamed or d.path in self.busy)]
        self._fill(self.main.query_one("#paired", DeviceTable), paired)
        self._fill(self.main.query_one("#nearby", DeviceTable), nearby)
        hidden = sum(1 for d in devices if not d.paired) - len(nearby)
        nb = self.main.query_one("#nearby", DeviceTable)
        nb.border_subtitle = (f"{hidden} unnamed hidden" + (" · \\[u] show" if self.size.width >= 50 else "")
                              if hidden and not show_unnamed else "")
        hint = self.main.query_one("#hint", Static)
        hint.display = not nearby
        if not nearby:
            scanning = a is not None and a.discovering
            hint.update("// scanning... put your device in pairing mode" if scanning
                        else "// no nearby devices. press \\[s] to scan")

    def _fill(self, table, devices):
        selected = getattr(table, "_bluestar_selected", None) or self._selected_key(table)
        table._bluestar_selected = None
        keys = [d.path for d in devices]
        current = [rk.value for rk in table.rows]
        if keys == current:
            for d in devices:
                for (key, _l, _w), value in zip(self.columns, self._row(d)):
                    table.update_cell(d.path, key, value)
            return
        table.clear()
        for d in devices:
            table.add_row(*self._row(d), key=d.path)
        if selected in keys:
            table.move_cursor(row=keys.index(selected), animate=False)

    @staticmethod
    def _selected_key(table):
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def focus_table(self, table, direction):
        tables = [self.main.query_one("#paired", DeviceTable), self.main.query_one("#nearby", DeviceTable)]
        idx = tables.index(table) + direction
        if 0 <= idx < len(tables) and tables[idx].row_count:
            target = tables[idx]
            target.focus()
            target.move_cursor(row=0 if direction > 0 else target.row_count - 1, animate=False)

    def log_message(self, message, error=False):
        stamp = time.strftime("%H:%M:%S")
        t = Text(no_wrap=True, overflow="ellipsis")
        if self.size.width >= 60:
            t.append(f"[{stamp}] ", style=GREEN_DARK)
        t.append(message, style=RED if error else GREEN_DIM)
        self.main.query_one("#log", Static).update(t)

    # -- selection helpers ------------------------------------------------------------------------

    def selected_device(self):
        focused = self.focused
        tables = [focused] if isinstance(focused, DeviceTable) else []
        tables += [self.main.query_one("#paired", DeviceTable), self.main.query_one("#nearby", DeviceTable)]
        for table in tables:
            key = self._selected_key(table)
            if key:
                return self.client.device(key)
        return None

    async def _run(self, device, busy_text, coro, ok_text=None, quiet_errors=()):
        if device is not None:
            self.busy[device.path] = busy_text
            self.refresh_all()
        try:
            await coro
        except BluezError as err:
            if err.short not in quiet_errors:
                self.log_message(f"{busy_text.rstrip('.')} failed: {err.message}", error=True)
            return False
        finally:
            if device is not None:
                self.busy.pop(device.path, None)
                self.refresh_all()
        if ok_text:
            self.log_message(ok_text)
        return True

    # -- actions --------------------------------------------------------------------------------

    def on_data_table_row_selected(self, _event):
        self.action_primary()

    def action_primary(self):
        self.run_worker(self._do_primary(), group="actions", exclusive=False)

    async def _do_primary(self):
        d = self.selected_device()
        if d is None or d.path in self.busy:
            return
        if d.connected:
            await self._disconnect(d)
        elif d.blocked:
            self.log_message(f"{d.alias} is blocked; press [b] to unblock", error=True)
        elif not d.paired:
            await self._pair_and_connect(d)
        else:
            await self._run(d, "connecting...", d.connect(), f"connected {d.alias}")

    async def _disconnect(self, d):
        await self._run(d, "disconnecting...", d.disconnect(), f"disconnected {d.alias}")

    def action_disconnect(self):
        self.run_worker(self._do_disconnect(), group="actions", exclusive=False)

    async def _do_disconnect(self):
        d = self.selected_device()
        if d is None or d.path in self.busy:
            return
        if not d.connected:
            self.log_message(f"{d.alias} is not connected")
            return
        await self._disconnect(d)

    async def _pair_and_connect(self, d):
        await self._stop_scan()  # discovery slows pairing down a lot
        ok = await self._run(d, "pairing...", d.pair(), quiet_errors=("AuthenticationCanceled",))
        if not ok:
            return
        try:
            await d.set_trusted(True)
        except BluezError:
            pass
        await self._run(d, "connecting...", d.connect(), f"paired and connected {d.alias}")

    def cancel_pairing(self):
        for path, text in list(self.busy.items()):
            if text.startswith("pairing"):
                d = self.client.device(path)
                if d:
                    self.run_worker(d.cancel_pairing(), exclusive=False)

    def action_scan(self):
        self.run_worker(self._do_scan(), group="actions", exclusive=False)

    async def _do_scan(self):
        a = self.adapter
        if a is None:
            return
        if not a.powered:
            self.log_message("bluetooth is off; press [p] to power on", error=True)
            return
        if self.scan_started:
            await self._stop_scan()
            self.log_message("scan stopped")
            return
        try:
            await a.start_discovery()
        except BluezError as err:
            if err.short != "InProgress":
                self.log_message(f"scan failed: {err.message}", error=True)
                return
        self.scan_started = time.monotonic()
        self.log_message("scanning... put your device in pairing mode")

    async def _stop_scan(self):
        """Stop the discovery *we* started (other programs may be scanning too)."""
        a = self.adapter
        started, self.scan_started = self.scan_started, None
        if a is None or not started:
            return
        # BlueZ answers InProgress while a start/stop is still pending; giving up there would
        # leave our discovery session running forever, so retry for a few seconds.
        for _ in range(20):
            try:
                await a.stop_discovery()
                return
            except BluezError as err:
                if err.short != "InProgress":
                    return  # e.g. "No discovery started": nothing left to stop
            await asyncio.sleep(0.25)
        self.log_message("could not stop scanning", error=True)

    def action_remove(self):
        self.run_worker(self._do_remove(), group="actions", exclusive=False)

    async def _do_remove(self):
        d = self.selected_device()
        if d is None:
            return
        fut = asyncio.get_running_loop().create_future()
        self.push_screen(ConfirmScreen(
            f"remove {d.alias}?",
            "the device will be disconnected and forgotten.\nyou will need to pair it again to use it.",
            "remove", "cancel", danger=True), fut.set_result)
        if not await fut:
            return
        a = d.adapter
        if a is not None:
            await self._run(d, "removing...", a.remove_device(d), f"removed {d.alias}")

    def action_trust(self):
        self.run_worker(self._do_trust(), group="actions", exclusive=False)

    async def _do_trust(self):
        d = self.selected_device()
        if d is not None:
            value = not d.trusted
            await self._run(None, "trust", d.set_trusted(value),
                            f"{d.alias}: {'trusted' if value else 'not trusted'}")

    def action_block(self):
        self.run_worker(self._do_block(), group="actions", exclusive=False)

    async def _do_block(self):
        d = self.selected_device()
        if d is not None:
            value = not d.blocked
            await self._run(None, "block", d.set_blocked(value),
                            f"{d.alias}: {'blocked' if value else 'unblocked'}")

    def action_rename(self):
        self.run_worker(self._do_rename(), group="actions", exclusive=False)

    async def _do_rename(self):
        d = self.selected_device()
        if d is None:
            return
        fut = asyncio.get_running_loop().create_future()
        self.push_screen(InputScreen("rename", f"new name for {d.address} (empty = device's own name)",
                                     value=d.alias), fut.set_result)
        name = await fut
        if name is not None:
            await self._run(None, "rename", d.set_alias(name.strip()), f"renamed to {name.strip() or d.name}")

    def action_info(self):
        d = self.selected_device()
        if d is not None:
            self.push_screen(InfoScreen(d))

    def action_power(self):
        self.run_worker(self._do_power(), group="actions", exclusive=False)

    async def _do_power(self):
        a = self.adapter
        if a is None:
            return
        value = not a.powered
        try:
            await set_powered(a, value)
        except BluezError as err:
            self.log_message(f"power {'on' if value else 'off'} failed: {err.message}", error=True)
            return
        self.log_message(f"bluetooth {'on' if value else 'off'}")

    def action_visible(self):
        self.run_worker(self._do_visible(), group="actions", exclusive=False)

    async def _do_visible(self):
        a = self.adapter
        if a is not None:
            value = not a.discoverable
            await self._run(None, "visibility", a.set_discoverable(value),
                            f"{'visible to' if value else 'hidden from'} other devices")

    def action_unnamed(self):
        self.settings["show-unnamed"] = not self.settings["show-unnamed"]
        self.refresh_all()
        self.log_message(("showing" if self.settings["show-unnamed"] else "hiding") + " unnamed devices")


def run():
    BluestarTUI().run()
    return 0
