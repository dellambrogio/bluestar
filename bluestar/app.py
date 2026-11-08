"""Command line entry point."""

import argparse
import sys

from . import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bluestar", description="Bluetooth manager for the terminal.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tray", action="store_true",
                       help="run in the background: tray icon, notifications, pairing agent")
    group.add_argument("--toggle", action="store_true",
                       help="open the TUI in a new terminal window, or close it if it is open")
    group.add_argument("--version", action="version", version=f"bluestar {__version__}")
    args = parser.parse_args(argv[1:] if argv else sys.argv[1:])

    if args.tray:
        from .daemon import run
        return run()
    if args.toggle:
        from .util import toggle_tui
        toggle_tui()
        return 0
    from .tui import run
    return run()
