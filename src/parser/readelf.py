"""Call arm-none-eabi-readelf and parse its text output."""

import re
import subprocess
from pathlib import Path
from typing import Optional

from src.core.models import (
    Symbol, BaseType, StructType, ArrayType, PointerType, EnumType,
    TypedefType, FuncType, MemberInfo, TypeInfo, Variable,
)

READELF = "arm-none-eabi-readelf"

# Regex for symbol table line: "   Num:    Value  Size Type    Bind   Vis      Ndx Name"
_SYM_RE = re.compile(
    r"\s*(?P<num>\d+):\s+"
    r"(?P<value>[0-9a-fA-F]+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<type>\S+)\s+"
    r"(?P<bind>\S+)\s+"
    r"(?P<vis>\S+)\s+"
    r"(?P<ndx>\S+)\s+"
    r"(?P<name>.+)"
)

# DIE header: " <N><hex>: Abbrev Number: X (DW_TAG_xxx)"
_DIE_RE = re.compile(r"^\s*<(?P<level>\d+)><(?P<offset>[0-9a-fA-F]+)>:\s+Abbrev Number:\s+\d+\s+\((?P<tag>\S+)\)")

# Attribute line: "    <hex>   DW_AT_name        : (indirect string, offset: 0x...): foo"
# or simpler: "    <hex>   DW_AT_byte_size   : 4"
# or location: "    <hex>   DW_AT_location    : 5 byte block: 3 0 0 0 20 \t(DW_OP_addr: 20000000)"
_ATTR_RE = re.compile(r"^\s+<[0-9a-fA-F]+>\s+(DW_AT_\S+)\s*:\s*(.*)")

# Type reference in attribute value: <0xhex>
_TYPE_REF_RE = re.compile(r"<(0x[0-9a-fA-F]+)>")

# DW_OP_addr comment from readelf
_OP_ADDR_RE = re.compile(r"DW_OP_addr:\s*([0-9a-fA-F]+)")

# DW_OP_plus_uconst in block expressions (e.g. "2 byte block: 23 4 (DW_OP_plus_uconst: 4)")
_BLOCK_PLUS_UCONST_RE = re.compile(r"DW_OP_plus_uconst:\s*(\d+)")

def _parse_int(raw: str, default: int = 0) -> int:
    """Safely parse an attribute value that may be hex (0x...), decimal, or a DWARF block expression."""
    if not raw or raw.strip() == "":
        return default
    s = raw.strip()
    # Hex or decimal integer
    if re.match(r'^\s*-?(?:0x[0-9a-fA-F]+|[0-9]+)\s*$', s):
        return int(s, 0)
    # DWARF block expression like "2 byte block: 23 4 (DW_OP_plus_uconst: 4)"
    m = _BLOCK_PLUS_UCONST_RE.search(s)
    if m:
        return int(m.group(1))
    return default


def run_readelf(filepath: str | Path, *args: str) -> str:
    result = subprocess.run(
        [READELF, *args, str(filepath)],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout


class RawDie:
    """Raw DIE entry from readelf output before type resolution."""
    __slots__ = ("offset", "tag", "attrs", "children")
    def __init__(self, offset: int, tag: str):
        self.offset = offset
        self.tag = tag
        self.attrs: dict[str, str] = {}
        self.children: list[RawDie] = []


class DwarfDB:
    """Indexed DWARF debug info database."""
    def __init__(self):
        self.types: dict[int, TypeInfo] = {}
        self.variables: list[Variable] = []
        self.structs: dict[str, StructType] = {}
        self._all_dies: dict[int, RawDie] = {}

    def has_debug_info(self) -> bool:
        return len(self._all_dies) > 0


# ------------------------------------------------------------------
# Symbol table parsing
# ------------------------------------------------------------------

def parse_symbol_table(filepath: str | Path) -> list[Symbol]:
    text = run_readelf(filepath, "-s")
    symbols = []
    in_symtab = False
    for line in text.splitlines():
        if ".symtab" in line and "Symbol table" in line:
            in_symtab = True
            continue
        if not in_symtab:
            continue
        # Stop at blank line or next section
        if not line.strip():
            if symbols:
                break
            continue
        m = _SYM_RE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        if not name:
            continue
        symbols.append(Symbol(
            name=name,
            address=int(m.group("value"), 16),
            size=int(m.group("size")),
            binding=m.group("bind"),
            sym_type=m.group("type"),
            section=m.group("ndx"),
        ))
    return symbols


# ------------------------------------------------------------------
# DWARF line table parsing (for source file names)
# ------------------------------------------------------------------

def parse_line_table(filepath: str | Path) -> dict[int, str]:
    """Parse readelf -wl output to get file name table.
    Returns {file_index: file_name} mapping.
    """
    text = run_readelf(filepath, "-wl")
    if "The File Name Table" not in text:
        return {}

    # Parse directory table
    dirs: dict[int, str] = {}
    dir_pattern = re.compile(r"^\s+(\d+)\t(.+)")
    file_pattern = re.compile(r"^\s+(\d+)\t(\d+)\t\d+\t\d+\t(.+)")

    in_dirs = False
    in_files = False
    files: dict[int, str] = {}

    for line in text.splitlines():
        if "The Directory Table" in line:
            in_dirs = True
            in_files = False
            continue
        if "The File Name Table" in line:
            in_dirs = False
            in_files = True
            continue
        if in_dirs and ("Line Number Statements" in line or "Opcodes:" in line or line.strip().startswith("Entry")):
            continue
        if in_files and "Line Number Statements" in line:
            break

        if in_dirs:
            m = dir_pattern.match(line)
            if m:
                dirs[int(m.group(1))] = m.group(2)

        if in_files:
            m = file_pattern.match(line)
            if m:
                idx = int(m.group(1))
                dir_idx = int(m.group(2))
                name = m.group(3)
                if dir_idx and dir_idx in dirs:
                    name = dirs[dir_idx].replace("\\", "/") + "/" + name
                files[idx] = name

    return files


# ------------------------------------------------------------------
# ELF header / section parsing
# ------------------------------------------------------------------

def get_elf_info(filepath: str | Path) -> dict:
    header_text = run_readelf(filepath, "-h")
    info: dict = {}
    header_patterns = {
        "machine": r"Machine:\s+(.+)",
        "entry": r"Entry point address:\s+(0x[0-9a-fA-F]+)",
        "bitness": r"Class:\s+(.+)",
        "endianness": r"Data:\s+(.+)",
        "type": r"Type:\s+(.+)",
    }
    for key, pat in header_patterns.items():
        m = re.search(pat, header_text)
        if m:
            info[key] = m.group(1)

    entry_str = info.get("entry", "0x0")
    info["entry_point"] = int(entry_str, 16) if entry_str.startswith("0x") else 0
    info["format"] = "ELF"
    return info


def get_section_info(filepath: str | Path) -> list[dict]:
    text = run_readelf(filepath, "-S")
    result = []
    in_sections = False
    # Section header format: "[Nr] Name              Type            Addr     Off    Size   ..."
    # We look for data lines after the header
    for line in text.splitlines():
        if "Nr" in line and "Name" in line and "Type" in line:
            in_sections = True
            continue
        if not in_sections:
            continue
        if not line.strip():
            break
        # Match section line: "  [ 0]                   NULL           00000000 000000 000000 ..."
        m = re.match(
            r"\s*\[\s*(?P<nr>\d+)\]\s+(?P<name>\S*)\s+(?P<type>\S+)\s+"
            r"(?P<addr>[0-9a-fA-F]+)\s+(?P<off>[0-9a-fA-F]+)\s+(?P<size>[0-9a-fA-F]+)\s+",
            line
        )
        if m:
            result.append({
                "index": int(m.group("nr")),
                "name": m.group("name"),
                "address": int(m.group("addr"), 16),
                "size": int(m.group("size"), 16),
                "type": m.group("type"),
            })
    return result


# ------------------------------------------------------------------
# DWARF debug info parsing
# ------------------------------------------------------------------

def parse_debug_info(filepath: str | Path) -> DwarfDB:
    """Parse readelf -wi output into a DwarfDB with resolved types."""
    text = run_readelf(filepath, "-wi")
    if "Contents of the .debug_info section" not in text:
        return DwarfDB()

    all_dies: dict[int, RawDie] = {}
    stack: list[RawDie] = []  # parent DIE stack

    for line in text.splitlines():
        die_m = _DIE_RE.match(line)
        if die_m:
            level = int(die_m.group("level"))
            offset = int(die_m.group("offset"), 16)
            tag = die_m.group("tag")
            die = RawDie(offset, tag)

            # Pop stack to maintain correct parent at current level
            while stack and len(stack) >= level:
                stack.pop()

            if stack:
                stack[-1].children.append(die)
            stack.append(die)
            all_dies[offset] = die
            continue

        attr_m = _ATTR_RE.match(line)
        if attr_m and stack:
            attr_name = attr_m.group(1)
            attr_value = attr_m.group(2).strip()
            stack[-1].attrs[attr_name] = attr_value
            continue

    # Build the database
    db = DwarfDB()
    db._all_dies = all_dies

    # Parse line table for source file name resolution
    file_table = parse_line_table(filepath)

    # Pass 1: collect all types (base, typedef, pointer, etc.)
    for offset, die in all_dies.items():
        ti = _die_to_type_info(die, all_dies, set())
        if ti is not None:
            db.types[offset] = ti

    # Pass 2: collect structs (named)
    for offset, die in all_dies.items():
        ti = db.types.get(offset)
        if isinstance(ti, StructType):
            if ti.name and ti.name != "<anonymous>":
                db.structs[ti.name] = ti

    # Pass 2.5: link typedef names to their underlying anonymous structs
    # e.g. "typedef struct { ... } IMU_Data_t;" → allow lookup by IMU_Data_t
    for offset, die in all_dies.items():
        ti = db.types.get(offset)
        if isinstance(ti, TypedefType):
            ut = ti.underlying_type
            while isinstance(ut, TypedefType):
                ut = ut.underlying_type
            if isinstance(ut, StructType):
                if ti.name and ti.name not in db.structs:
                    db.structs[ti.name] = ut

    # Pass 3: collect variables
    for offset, die in all_dies.items():
        if die.tag != "DW_TAG_variable":
            continue
        name = _attr_value_stripped(die.attrs.get("DW_AT_name", ""))
        if not name:
            continue
        addr = _parse_location(die.attrs.get("DW_AT_location", ""))
        type_offset = _parse_type_ref(die.attrs.get("DW_AT_type", ""))
        type_info = db.types.get(type_offset) if type_offset else None
        is_decl = die.attrs.get("DW_AT_declaration", "") != ""

        # Include declarations (addr=0) with type info for later merging
        if addr == 0 and (is_decl or not type_info):
            if not type_info:
                continue
            # Declaration with type info - include with addr=0
        elif addr == 0:
            continue  # locals/unresolved without type

        size = _estimate_size(type_info)

        # Resolve source file name
        decl_file_str = die.attrs.get("DW_AT_decl_file", "")
        file_name = ""
        if decl_file_str:
            try:
                file_idx = int(decl_file_str)
                file_name = file_table.get(file_idx, "")
            except ValueError:
                pass

        db.variables.append(Variable(
            name=name, address=addr, size=size, type_info=type_info,
            file_name=file_name,
        ))

    return db


def _die_to_type_info(die: RawDie, all_dies: dict[int, RawDie], visiting: set[int]) -> Optional[TypeInfo]:
    """Convert a RawDie to a TypeInfo, recursing for type references."""
    offset = die.offset
    if offset in visiting:
        return None  # circular ref
    visiting.add(offset)

    try:
        tag = die.tag

        if tag == "DW_TAG_base_type":
            name = _attr_value_stripped(die.attrs.get("DW_AT_name", ""))
            size = _parse_int(die.attrs.get("DW_AT_byte_size", "0"))
            enc_str = die.attrs.get("DW_AT_encoding", "")
            encoding = _attr_value_stripped(enc_str)
            return BaseType(name=name, byte_size=size, encoding=encoding)

        if tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
            name = _attr_value_stripped(die.attrs.get("DW_AT_name", ""))
            size = int(die.attrs.get("DW_AT_byte_size", "0"))
            is_union = tag == "DW_TAG_union_type"
            members = []
            for child in die.children:
                if child.tag in ("DW_TAG_member", "DW_TAG_inheritance"):
                    m_name = _attr_value_stripped(child.attrs.get("DW_AT_name", ""))
                    m_offset = _parse_int(child.attrs.get("DW_AT_data_member_location", "0"))
                    m_type_ref = _parse_type_ref(child.attrs.get("DW_AT_type", ""))
                    m_type = None
                    if m_type_ref and m_type_ref in all_dies:
                        m_type = _die_to_type_info(all_dies[m_type_ref], all_dies, visiting)
                    bit_size = _parse_int(child.attrs.get("DW_AT_bit_size", "0"))
                    bit_offset = _parse_int(child.attrs.get("DW_AT_bit_offset", "0"))
                    members.append(MemberInfo(
                        name=m_name, offset=m_offset, type_info=m_type,
                        bit_size=bit_size, bit_offset=bit_offset,
                    ))
            return StructType(name=name, size=size, members=members, is_union=is_union)

        if tag == "DW_TAG_enumeration_type":
            name = _attr_value_stripped(die.attrs.get("DW_AT_name", ""))
            size = _parse_int(die.attrs.get("DW_AT_byte_size", "0"))
            values = []
            for child in die.children:
                if child.tag == "DW_TAG_enumerator":
                    c_name = _attr_value_stripped(child.attrs.get("DW_AT_name", ""))
                    c_val = _parse_int(child.attrs.get("DW_AT_const_value", "0"))
                    values.append((c_name, c_val))
            return EnumType(name=name, size=size, values=values)

        if tag == "DW_TAG_typedef":
            name = _attr_value_stripped(die.attrs.get("DW_AT_name", ""))
            type_ref = _parse_type_ref(die.attrs.get("DW_AT_type", ""))
            underlying = None
            if type_ref and type_ref in all_dies:
                underlying = _die_to_type_info(all_dies[type_ref], all_dies, visiting)
            return TypedefType(name=name, underlying_type=underlying)

        if tag == "DW_TAG_pointer_type":
            type_ref = _parse_type_ref(die.attrs.get("DW_AT_type", ""))
            pointed = None
            if type_ref and type_ref in all_dies:
                pointed = _die_to_type_info(all_dies[type_ref], all_dies, visiting)
            return PointerType(pointed_type=pointed, size=4)  # ARM 32-bit

        if tag == "DW_TAG_array_type":
            type_ref = _parse_type_ref(die.attrs.get("DW_AT_type", ""))
            elem_type = None
            if type_ref and type_ref in all_dies:
                elem_type = _die_to_type_info(all_dies[type_ref], all_dies, visiting)
            count = 0
            for child in die.children:
                if child.tag == "DW_TAG_subrange_type":
                    upper = child.attrs.get("DW_AT_upper_bound", "")
                    cnt = child.attrs.get("DW_AT_count", "")
                    if upper:
                        count = _parse_int(upper, 0) + 1
                    elif cnt:
                        count = _parse_int(cnt, 0)
            elem_size = _estimate_size(elem_type) if elem_type else 0
            return ArrayType(element_type=elem_type, count=count, total_size=count * elem_size)

        if tag in ("DW_TAG_const_type", "DW_TAG_volatile_type", "DW_TAG_restrict_type"):
            type_ref = _parse_type_ref(die.attrs.get("DW_AT_type", ""))
            if type_ref and type_ref in all_dies:
                return _die_to_type_info(all_dies[type_ref], all_dies, visiting)
            return None

        if tag == "DW_TAG_unspecified_type":
            return BaseType(name="void", byte_size=0, encoding="void")

        if tag == "DW_TAG_subroutine_type":
            type_ref = _parse_type_ref(die.attrs.get("DW_AT_type", ""))
            return_type = None
            if type_ref and type_ref in all_dies:
                return_type = _die_to_type_info(all_dies[type_ref], all_dies, visiting)
            return FuncType(return_type=return_type)

        return None
    finally:
        visiting.discard(offset)


def _attr_value_stripped(raw: str) -> str:
    """Extract the actual value from an attribute string.
    Handles: '(indirect string, offset: 0x...): foo' -> 'foo'
             '(index string: 0x...): bar' -> 'bar'
             '4' -> '4'
             'signed char' -> 'signed char'
    """
    raw = raw.strip()
    # Remove trailing parenthesized annotation like "(DW_OP_addr: ...)"
    if "\t" in raw:
        raw = raw.split("\t")[0].strip()
    # Check for indirect/index string: "(indirect string, offset: 0x...): value"
    if raw.startswith("("):
        colon_idx = raw.rfind("): ")
        if colon_idx >= 0:
            return raw[colon_idx + 2:].strip().strip('"')
        # Just a parenthesized value like "(signed char)"
        return raw.strip("()").strip().strip('"')
    return raw.strip().strip('"')


def _parse_type_ref(raw: str) -> Optional[int]:
    """Parse DW_AT_type value to get the referenced DIE offset.
    Handles: '<0x3d>' -> 0x3d
             '(indirect string, offset: ...): <0x3d>' -> 0x3d
    """
    m = _TYPE_REF_RE.search(raw)
    if m:
        return int(m.group(1), 16)
    return None


def _parse_location(raw: str) -> int:
    """Parse DW_AT_location to extract the memory address (only for global DW_OP_addr).
    readelf provides the decoded address in a comment: (DW_OP_addr: 20000000)
    """
    m = _OP_ADDR_RE.search(raw)
    if m:
        addr = int(m.group(1), 16)
        return addr if addr != 0 else 0
    return 0


def _estimate_size(type_info: Optional[TypeInfo]) -> int:
    if type_info is None:
        return 0
    if isinstance(type_info, BaseType):
        return type_info.byte_size
    if isinstance(type_info, StructType):
        return type_info.size
    if isinstance(type_info, ArrayType):
        return type_info.total_size
    if isinstance(type_info, PointerType):
        return type_info.size
    if isinstance(type_info, EnumType):
        return type_info.size
    if isinstance(type_info, TypedefType):
        return _estimate_size(type_info.underlying_type)
    return 0
