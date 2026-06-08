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
        seen_addrs: dict[int, int] = {}  # addr -> index into variables

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

            # LTO: strip .N suffix from name if DWARF provides the correct base name
            display_name = sym.name
            if dv and dv.name != sym.name and _match_lto_suffix(sym.name, dv.name):
                display_name = dv.name

            # LTO dedup: same address — prefer DWARF-named entry
            prev_idx = seen_addrs.get(sym.address)
            if prev_idx is not None:
                prev_var = variables[prev_idx]
                prev_has_type = prev_var.type_info is not None
                cur_has_type = type_info is not None
                prev_is_lto = bool(_LTO_SUFFIX_RE.match(prev_var.name))
                cur_is_lto = bool(_LTO_SUFFIX_RE.match(display_name))
                # Replace previous entry if current is strictly better
                if (not prev_has_type and cur_has_type) or (prev_is_lto and not cur_is_lto):
                    variables[prev_idx] = Variable(
                        name=display_name, address=sym.address, size=size,
                        type_info=type_info, symbol=sym, file_name=file_name,
                    )
                    if prev_var.name in seen_names:
                        seen_names.discard(prev_var.name)
                    seen_names.add(display_name)
                    seen_addrs[sym.address] = prev_idx
                continue

            variables.append(Variable(
                name=display_name,
                address=sym.address,
                size=size,
                type_info=type_info,
                symbol=sym,
                file_name=file_name,
            ))
            seen_names.add(display_name)
            seen_addrs[sym.address] = len(variables) - 1

        # DWARF-only variables
        if self.dwarf_db and self.dwarf_db.has_debug_info():
            for dv in self.dwarf_db.variables:
                if dv.name in seen_names:
                    continue
                if dv.address == 0:
                    continue
                # LTO dedup by address: update existing entry with DWARF name/type
                prev_idx = seen_addrs.get(dv.address)
                if prev_idx is not None:
                    prev_var = variables[prev_idx]
                    prev_has_type = prev_var.type_info is not None
                    prev_is_lto = bool(_LTO_SUFFIX_RE.match(prev_var.name))
                    # DWARF name is better — upgrade
                    if not prev_has_type or prev_is_lto:
                        if prev_var.name in seen_names:
                            seen_names.discard(prev_var.name)
                        seen_names.add(dv.name)
                        variables[prev_idx] = Variable(
                            name=dv.name, address=dv.address, size=dv.size,
                            type_info=dv.type_info, symbol=prev_var.symbol,
                            file_name=_clean_dwarf_path(dv.file_name) or prev_var.file_name,
                        )
                        seen_addrs[dv.address] = prev_idx
                    continue

                map_file = self.symbol_to_file.get(dv.name, "")
                if map_file:
                    dv.file_name = map_file
                elif dv.file_name:
                    dv.file_name = _clean_dwarf_path(dv.file_name)
                variables.append(dv)
                seen_names.add(dv.name)
                seen_addrs[dv.address] = len(variables) - 1

        variables.sort(key=lambda v: v.address)
        return variables


import re as _re

# LTO generates duplicates like "var.3", "var.4" for "var" across CUs
_LTO_SUFFIX_RE = _re.compile(r"^(.*)(?:\.\d+)$")

def _match_lto_suffix(suffixed: str, base: str) -> bool:
    """Return True if `suffixed` is base with a .N suffix (e.g. 'foo.4' matches 'foo')."""
    m = _LTO_SUFFIX_RE.match(suffixed)
    return m is not None and m.group(1) == base


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
