from src.core.models import Variable
from src.parser.readelf import DwarfDB


class VariableInventory:
    def __init__(self, elf_parser, dwarf_db: DwarfDB = None, symbol_to_file: dict[str, str] = None):
        self.elf_parser = elf_parser
        self.dwarf_db = dwarf_db
        self.symbol_to_file = symbol_to_file or {}

    def generate(self) -> list[Variable]:
        symbols = self.elf_parser.get_symbols()

        dwarf_by_addr: dict[int, Variable] = {}
        dwarf_by_name: dict[str, Variable] = {}
        if self.dwarf_db and self.dwarf_db.has_debug_info():
            for v in self.dwarf_db.variables:
                if v.address != 0:
                    dwarf_by_addr[v.address] = v
                # Name match: definition (addr!=0) takes priority over declaration
                if v.name not in dwarf_by_name or v.address != 0:
                    dwarf_by_name[v.name] = v

        variables: list[Variable] = []
        seen_names: set[str] = set()

        for sym in symbols:
            if sym.sym_type != "OBJECT":
                continue
            if sym.address == 0:
                continue

            type_info = None
            file_name = self.symbol_to_file.get(sym.name, "")
            dv = dwarf_by_addr.get(sym.address) or dwarf_by_name.get(sym.name)
            if dv and dv.type_info:
                type_info = dv.type_info
                size = dv.size
                if not file_name:
                    file_name = _clean_dwarf_path(dv.file_name)
            else:
                size = sym.size

            variables.append(Variable(
                name=sym.name,
                address=sym.address,
                size=size,
                type_info=type_info,
                symbol=sym,
                file_name=file_name,
            ))
            seen_names.add(sym.name)

        # DWARF-only variables
        if self.dwarf_db and self.dwarf_db.has_debug_info():
            for dv in self.dwarf_db.variables:
                if dv.name not in seen_names and dv.address != 0:
                    map_file = self.symbol_to_file.get(dv.name, "")
                    if map_file:
                        dv.file_name = map_file
                    elif dv.file_name:
                        dv.file_name = _clean_dwarf_path(dv.file_name)
                    variables.append(dv)
                    seen_names.add(dv.name)

        variables.sort(key=lambda v: v.address)
        return variables


def _clean_dwarf_path(path: str) -> str:
    """Clean up DWARF source file paths for display."""
    if not path:
        return ""
    from pathlib import PurePosixPath as PPP
    p = PPP(path)
    # Header files and include directories: just the filename
    if p.suffix == ".h" or "/include/" in path or "/Include/" in path:
        return p.name
    # Strip absolute prefixes to find project-relative path
    for marker in ["/Core/", "/Drivers/", "/lib/", "/app/", "/bsp/"]:
        idx = path.find(marker)
        if idx >= 0:
            return path[idx + 1:]
    # Fallback: just the filename
    return p.name
