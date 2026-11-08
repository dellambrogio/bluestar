# bluestar

A small Bluetooth manager for Linux — an alternative to blueman.
GTK4 + libadwaita on top of BlueZ's D-Bus API, with a green-on-black terminal look.

```
> bluestar_
[ paired ]   Headphones  connected  [ Disconnect ]
             Speaker                 [  Connect   ]
[ nearby ]   // press scan to look for devices.
```

## Features

- **Scan** for nearby devices (auto-stops after 60 s)
- **Pair + connect** in one click, with prompts for PIN / passkey / confirmation codes (built-in BlueZ pairing agent)
- **Connect / disconnect** paired devices, battery level shown when the device reports it
- **Remove**, trust, block and rename devices; details dialog with supported profiles
- **Adapter** power switch (unblocks rfkill if needed), visibility, pairable, name
- **Tray icon** (StatusNotifierItem, no libappindicator needed — works with waybar, KDE, …):
  left-click toggles the window, right-click menu to power on/off and connect paired devices,
  notifications when devices connect/disconnect

## Requirements

- BlueZ 5 (`bluetoothd` running)
- Python 3 with PyGObject, GTK 4 and libadwaita ≥ 1.6

On Debian/Ubuntu:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 bluez
```

## Usage

```sh
git clone https://github.com/dellambrogio/bluestar.git
cd bluestar
./bin/bluestar            # open the window
./bin/bluestar --tray     # run in the background with a tray icon + pairing agent
./bin/bluestar --toggle   # show/hide the window of the running instance
```

Link it into your `PATH` if you like: `ln -s "$PWD/bin/bluestar" ~/.local/bin/bluestar`.

### sway / waybar

```
# ~/.config/sway/config
exec sleep 2 && ~/.local/bin/bluestar --tray
for_window [app_id="io.github.bluestar.Bluestar"] floating enable, resize set 800 600, move position mouse, move up 40 px
```

```jsonc
// ~/.config/waybar/config.jsonc
"bluetooth": {
    "on-click": "~/.local/bin/bluestar --toggle"
}
```

If you were using blueman, stop `blueman-applet` so the two pairing agents don't compete.

## Layout

| File | Purpose |
| --- | --- |
| `bluestar/bluez.py` | GObject wrapper around `org.bluez` (adapters, devices, battery) |
| `bluestar/window.py` | main window, device rows, dialogs |
| `bluestar/agent.py` | `org.bluez.Agent1` pairing agent |
| `bluestar/tray.py` | StatusNotifierItem + dbusmenu tray icon |
| `bluestar/app.py` | application, command line, notifications |
| `bluestar/style.css` | the green-on-black theme |

Settings live in `~/.config/bluestar/settings.json`.

## License

GPL-3.0-or-later
