"""Parse GNU ld linker map file to map symbol names to source files."""

import re
import os
from pathlib import Path


def parse_map_file(filepath: str | Path) -> dict[str, str]:
    """Parse a linker map file and return {symbol_name: source_filename}.

    Extracts the source .c file name from object file paths like:
      CMakeFiles/Gimbal_G0B1.dir/lib/app/gimbal.c.obj  →  lib/app/gimbal.c
    """
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # Start from "Linker script and memory map" section
    marker = "Linker script and memory map"
    idx = text.find(marker)
    if idx == -1:
        return {}
    lines = text[idx:].splitlines()

    symbol_to_file: dict[str, str] = {}
    current_obj: str = ""

    # Regex for lines starting with " ." (section lines)
    # Format: " .bss.varname   0xADDR       SIZE  objfile"
    section_re = re.compile(
        r"^\s+\.(?P<section>[a-zA-Z0-9_]+)(?:\.(?P<varname>[a-zA-Z_]\w*))?"
        r"\s+(?P<addr>0x[0-9a-fA-F]+)?\s*(?P<size>0x[0-9a-fA-F]+|\d+)?\s*(?P<obj>.+\.o(bj)?)?$"
    )

    # Regex for lines starting with spaces then an address
    # Format 1 (obj file): "                0xADDR        SIZE  objfile"
    # Format 2 (symbol):   "                0xADDR                symbolname"
    addr_re = re.compile(
        r"^\s+(?P<addr>0x[0-9a-fA-F]+)\s+"
        r"(?:(?P<size>0x[0-9a-fA-F]+|\d+)\s+)?"
        r"(?P<rest>.+)$"
    )

    for line in lines:
        # Skip assignment/alias lines like "_sdata = ."
        if " = " in line:
            continue
        # Skip alignment directives
        if "ALIGN" in line:
            continue
        # Skip fill lines
        if "*fill*" in line:
            continue

        # Section line (starts with space + dot)
        if re.match(r"^\s+\.", line):
            m = section_re.match(line)
            if m:
                obj = m.group("obj")
                varname = m.group("varname")
                if obj:
                    source_file = _extract_source(obj)
                    current_obj = source_file if source_file else current_obj
                    if varname and source_file:
                        symbol_to_file[varname] = source_file
                elif varname:
                    # Section with varname but no obj — record with current context
                    if current_obj:
                        symbol_to_file[varname] = current_obj
            continue

        # Address line
        m = addr_re.match(line)
        if not m:
            continue

        rest = m.group("rest").strip()
        if not rest:
            continue

        # Check if this is an obj file line (last token is .o or .obj path)
        if rest.endswith(".o") or rest.endswith(".obj") or ".o)" in rest or ".obj)" in rest:
            source_file = _extract_source(rest)
            if source_file:
                current_obj = source_file
        else:
            # Symbol line — rest is the symbol name
            symbol_name = rest.strip()
            # Filter out non-symbol entries
            if symbol_name and not symbol_name.startswith(".") and not symbol_name.startswith("0x"):
                if current_obj:
                    symbol_to_file[symbol_name] = current_obj

    return symbol_to_file


def _extract_source(obj_path: str) -> str:
    """Extract source .c filename from an object file path.

    Handles paths like:
      CMakeFiles/Gimbal_G0B1.dir/lib/app/gimbal.c.obj  →  lib/app/gimbal.c
      cmake/.../STM32_Drivers.dir/__/__/Core/Src/system_stm32g0xx.c.obj  →  Core/Src/system_stm32g0xx.c
    Returns empty string for library/system paths.
    """
    obj_path = obj_path.strip()
    # Remove trailing archive member notation like "(symbol)"
    obj_path = re.sub(r"\s*\(.*\)$", "", obj_path)

    # Check if it looks like a user source file (contains .dir/ pattern)
    if ".dir" not in obj_path:
        return ""

    # Find the .dir/ marker and extract everything after it
    parts = obj_path.split(".dir/", 1)
    if len(parts) != 2:
        return ""

    source_part = parts[1]
    # Remove trailing .obj or .o
    source_part = re.sub(r"\.(obj|o)$", "", source_part)

    # Replace __/ with ../ — CMake uses __ for path separators when going up
    # The pattern in the path is /__/ for ../
    source_part = source_part.replace("/__/", "/../")
    # Also handle leading __/
    if source_part.startswith("__/"):
        source_part = "../" + source_part[3:]

    # Normalize the path (resolve ..)
    try:
        normalized = os.path.normpath(source_part)
    except (ValueError, OSError):
        return ""

    # Strip leading ../ sequences (CMake paths may go above project root)
    normalized = normalized.replace("\\", "/")
    while normalized.startswith("../"):
        normalized = normalized[3:]

    if normalized.endswith(".c"):
        return normalized

    return ""
