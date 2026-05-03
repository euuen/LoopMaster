#!/usr/bin/env python3
"""LoopMaster — ELF/AXF symbol & struct memory layout analyzer with live scope."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    # If first arg is "scope", run GUI scope
    if len(sys.argv) > 1 and sys.argv[1] == "scope":
        import argparse
        parser = argparse.ArgumentParser(prog="loopmaster scope", description="MCU Variable Oscilloscope")
        parser.add_argument("scope", help="(reserved)")
        parser.add_argument("elf", type=str, nargs="?", default=None, help="Path to ELF/AXF file (optional)")
        parser.add_argument("--pack", type=str, default=None, help="Path to CMSIS-Pack file")
        parser.add_argument("--target", type=str, default=None, help="pyOCD target name")
        args = parser.parse_args()

        from src.ui.gui import run_scope
        run_scope(args.elf, pack_path=args.pack, target=args.target)
    else:
        from src.ui.cli import main as cli_main
        cli_main()


if __name__ == "__main__":
    main()
