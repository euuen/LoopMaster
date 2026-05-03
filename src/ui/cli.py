import argparse
import sys
from pathlib import Path

import yaml

from src.parser.elf_parser import ELFParser
from src.parser.readelf import parse_debug_info
from src.parser.variable_inventory import VariableInventory
from src.parser.struct_layout import StructLayoutEngine
from src.utils.exporters import VariableTableFormatter, StructLayoutFormatter


def load_config(config_path: Path) -> dict:
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loopmaster",
        description="LoopMaster — ELF/AXF symbol & struct analyzer",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    pi = sub.add_parser("info", help="Show ELF/AXF file overview")
    pi.add_argument("file", type=str, help="Path to ELF/AXF file")

    sym = sub.add_parser("symbols", help="List symbols from symbol table")
    sym.add_argument("file", type=str, help="Path to ELF/AXF file")
    sym.add_argument("--internal", action="store_true", help="Include internal symbols")
    sym.add_argument("-o", "--output", default="table", choices=["table", "csv", "json"],
                     help="Output format (default: table)")
    sym.add_argument("--output-file", type=str, default=None, help="Write output to file")

    var = sub.add_parser("variables", help="List global variables with type info")
    var.add_argument("file", type=str, help="Path to ELF/AXF file")
    var.add_argument("--internal", action="store_true", help="Include internal symbols")
    var.add_argument("-o", "--output", default="table", choices=["table", "csv", "json", "excel"],
                     help="Output format (default: table)")
    var.add_argument("--output-file", type=str, default=None, help="Write output to file")
    var.add_argument("--filter", type=str, default=None, dest="filter_pat", help="Filter by name substring")
    var.add_argument("--sort", default="address", choices=["name", "address", "size"], help="Sort key")

    st = sub.add_parser("struct", help="Show struct/union memory layout")
    st.add_argument("file", type=str, help="Path to ELF/AXF file")
    st.add_argument("-n", "--name", type=str, default=None, help="Struct/union name")
    st.add_argument("--list", action="store_true", help="List all struct/union names")
    st.add_argument("-o", "--output", default="table", choices=["table", "csv", "json"],
                     help="Output format (default: table)")
    st.add_argument("--output-file", type=str, default=None, help="Write output to file")
    st.add_argument("--nested", type=str, default=None, help="Expand nested struct layout")

    parser.add_argument("-c", "--config", type=str, default="config/settings.yaml",
                        help="Config file path")
    return parser


def write_output(content: str, filepath: str | None, fmt: str):
    if filepath:
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"Output written to {filepath}")
    else:
        print(content)


def handle_info(args):
    with ELFParser(args.file) as elf:
        info = elf.get_header_info()
        print(f"File       : {args.file}")
        print(f"Machine    : {info.get('machine', '?')}")
        print(f"Bitness    : {info.get('bitness', '?')}")
        print(f"Endianness : {info.get('endianness', '?')}")
        print(f"Entry      : 0x{info.get('entry_point', 0):08X}")
        print(f"Type       : {info.get('type', '?')}")
        print()
        sections = elf.get_section_info()
        print(f"{'#':<4} {'Name':<24} {'Address':<12} {'Size':<10} {'Type'}")
        print("-" * 70)
        for s in sections:
            print(f"{s['index']:<4} {s['name']:<24} 0x{s['address']:08X}   {s['size']:<8} {s['type']}")


def handle_symbols(args):
    with ELFParser(args.file) as elf:
        symbols = elf.get_symbols(include_internal=args.internal)
        from src.core.models import Variable
        var_list = [Variable(name=s.name, address=s.address, size=s.size, symbol=s) for s in symbols]

        fmt = VariableTableFormatter()
        if args.output == "table":
            fmt.to_rich_table(var_list)
        elif args.output == "csv":
            content = fmt.to_csv_string(var_list)
            write_output(content, args.output_file, args.output)
        elif args.output == "json":
            content = fmt.to_json_string(var_list)
            write_output(content, args.output_file, args.output)


def handle_variables(args):
    with ELFParser(args.file) as elf:
        dwarf_db = parse_debug_info(args.file)
        if not dwarf_db.has_debug_info():
            print("Warning: no DWARF debug info found, showing symbols only", file=sys.stderr)

        inventory = VariableInventory(elf, dwarf_db)
        var_list = inventory.generate()

        if args.filter_pat:
            pat = args.filter_pat.lower()
            var_list = [v for v in var_list if pat in v.name.lower()]

        if args.sort == "name":
            var_list.sort(key=lambda v: v.name)
        elif args.sort == "size":
            var_list.sort(key=lambda v: v.size)
        else:
            var_list.sort(key=lambda v: v.address)

        fmt = VariableTableFormatter()
        if args.output == "table":
            fmt.to_rich_table(var_list)
        elif args.output == "csv":
            content = fmt.to_csv_string(var_list)
            write_output(content, args.output_file, args.output)
        elif args.output == "json":
            content = fmt.to_json_string(var_list)
            write_output(content, args.output_file, args.output)
        elif args.output == "excel":
            if not args.output_file:
                print("Error: --output-file is required for excel format", file=sys.stderr)
                sys.exit(1)
            fmt.to_excel(var_list, args.output_file)
            print(f"Excel written to {args.output_file}")


def handle_struct(args):
    if not args.list and not args.name and not args.nested:
        print("Error: specify --name <struct> or --list", file=sys.stderr)
        sys.exit(1)

    dwarf_db = parse_debug_info(args.file)
    if not dwarf_db.has_debug_info():
        print("Error: no DWARF debug info found in this file", file=sys.stderr)
        sys.exit(1)

    engine = StructLayoutEngine(dwarf_db)

    if args.list:
        names = engine.list_all_structs()
        print(f"Found {len(names)} struct(s)/union(s):")
        for n in names:
            print(f"  {n}")
        return

    name = args.nested or args.name
    if not name:
        print("Error: specify --name", file=sys.stderr)
        sys.exit(1)

    layout = engine.get_layout(name)
    if layout is None:
        print(f"Error: struct/union '{name}' not found in DWARF info", file=sys.stderr)
        sys.exit(1)

    rows = layout.to_rows()
    fmt = StructLayoutFormatter()
    if args.output == "table":
        fmt.to_rich_table(rows)
    elif args.output == "csv":
        content = fmt.to_csv_string(rows)
        write_output(content, args.output_file, args.output)
    elif args.output == "json":
        content = fmt.to_json_string(rows)
        write_output(content, args.output_file, args.output)


def main():
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "info":
            handle_info(args)
        elif args.command == "symbols":
            handle_symbols(args)
        elif args.command == "variables":
            handle_variables(args)
        elif args.command == "struct":
            handle_struct(args)
    except FileNotFoundError:
        print(f"Error: file not found", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
