from dataclasses import dataclass
from typing import Optional

from src.core.models import (
    BaseType, StructType, ArrayType, PointerType, EnumType, TypedefType, TypeInfo,
)
from src.parser.readelf import DwarfDB


@dataclass
class LayoutRow:
    offset: int
    name: str
    type_str: str
    size: int
    bit_size: int = 0
    bit_offset: int = 0
    is_padding: bool = False
    is_total: bool = False


class StructLayoutEngine:
    def __init__(self, dwarf_db: DwarfDB):
        self.dwarf_db = dwarf_db

    def get_layout(self, struct_name: str) -> Optional["StructLayout"]:
        struct_type = self.dwarf_db.structs.get(struct_name)
        if struct_type is None:
            return None
        return StructLayout(struct_type, self.dwarf_db)

    def list_all_structs(self) -> list[str]:
        return sorted(self.dwarf_db.structs.keys())


class StructLayout:
    def __init__(self, struct_type: StructType, dwarf_db: DwarfDB):
        self.struct_type = struct_type
        self.dwarf_db = dwarf_db

    def to_rows(self) -> list[LayoutRow]:
        rows = []
        members = sorted(self.struct_type.members, key=lambda m: m.offset)

        if self.struct_type.is_union:
            for m in members:
                type_str = self._format_type(m.type_info)
                rows.append(LayoutRow(
                    offset=0, name=m.name, type_str=type_str,
                    size=self._type_size(m.type_info),
                    bit_size=m.bit_size, bit_offset=m.bit_offset,
                ))
            rows.append(LayoutRow(
                offset=0, name="TOTAL (union)", type_str="",
                size=self.struct_type.size, is_total=True,
            ))
            return rows

        current_pos = 0
        for m in members:
            if m.offset > current_pos:
                pad_size = m.offset - current_pos
                rows.append(LayoutRow(
                    offset=current_pos, name="<padding>",
                    type_str="---", size=pad_size, is_padding=True,
                ))
            type_str = self._format_type(m.type_info)
            member_size = self._type_size(m.type_info)
            rows.append(LayoutRow(
                offset=m.offset, name=m.name, type_str=type_str,
                size=max(member_size, 1),
                bit_size=m.bit_size, bit_offset=m.bit_offset,
            ))
            current_pos = m.offset + max(member_size, 1)

        if current_pos < self.struct_type.size:
            tail_pad = self.struct_type.size - current_pos
            rows.append(LayoutRow(
                offset=current_pos, name="<padding>",
                type_str="---", size=tail_pad, is_padding=True,
            ))

        rows.append(LayoutRow(
            offset=0, name="TOTAL",
            type_str=f"sizeof({self.struct_type.name})",
            size=self.struct_type.size, is_total=True,
        ))
        return rows

    def find_nested(self, name: str) -> Optional["StructLayout"]:
        st = self.dwarf_db.structs.get(name)
        if st:
            return StructLayout(st, self.dwarf_db)
        return None

    def _format_type(self, type_info: Optional[TypeInfo]) -> str:
        if type_info is None:
            return "void"
        if isinstance(type_info, BaseType):
            return type_info.name
        if isinstance(type_info, StructType):
            prefix = "union " if type_info.is_union else "struct "
            return f"{prefix}{type_info.name}"
        if isinstance(type_info, ArrayType):
            elem = self._format_type(type_info.element_type)
            return f"{elem}[{type_info.count}]"
        if isinstance(type_info, PointerType):
            pointed = self._format_type(type_info.pointed_type) if type_info.pointed_type else "void"
            return f"{pointed}*"
        if isinstance(type_info, EnumType):
            return f"enum {type_info.name}"
        if isinstance(type_info, TypedefType):
            return type_info.name
        return "?"

    @staticmethod
    def _type_size(type_info: Optional[TypeInfo]) -> int:
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
            return StructLayout._type_size(type_info.underlying_type)
        return 0
