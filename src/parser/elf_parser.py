"""ELF/AXF file info and symbol parsing via arm-none-eabi-readelf."""

from pathlib import Path
from typing import Optional

from src.core.models import Symbol
from src.parser.readelf import (
    get_elf_info as _get_elf_info,
    get_section_info as _get_section_info,
    parse_symbol_table as _parse_symbol_table,
)


class ELFParser:
    def __init__(self, filepath: str | Path):
        self.filepath = Path(filepath)

    def open(self) -> "ELFParser":
        return self

    def close(self):
        pass

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def get_header_info(self) -> dict:
        return _get_elf_info(self.filepath)

    def get_section_info(self) -> list[dict]:
        return _get_section_info(self.filepath)

    def get_symbols(
        self,
        include_internal: bool = False,
        exclude_types: Optional[list[str]] = None,
    ) -> list[Symbol]:
        if exclude_types is None:
            exclude_types = ["NOTYPE", "SECTION", "FILE"]
        symbols = _parse_symbol_table(self.filepath)
        result = []
        for s in symbols:
            if s.sym_type in exclude_types:
                continue
            if not include_internal and (
                s.name.startswith(".L") or s.name.startswith("$")
            ):
                continue
            result.append(s)
        result.sort(key=lambda s: s.address)
        return result
