import csv
import io
import json
from typing import Optional

from src.core.models import (
    BaseType, StructType, ArrayType, PointerType, EnumType, TypedefType,
    Variable, TypeInfo,
)
from src.parser.struct_layout import LayoutRow


class VariableTableFormatter:
    def to_rich_table(self, variables: list[Variable]):
        from rich.table import Table
        from rich.console import Console

        table = Table(title="Variable Inventory", border_style="cyan")
        table.add_column("Address", style="green")
        table.add_column("Name", style="white")
        table.add_column("Type", style="yellow")
        table.add_column("Size", style="magenta", justify="right")

        for v in variables:
            table.add_row(
                f"0x{v.address:08X}",
                v.name,
                self._format_type(v.type_info),
                str(v.size),
            )

        console = Console()
        console.print(table)

    def to_text_table(self, variables: list[Variable]) -> str:
        lines = []
        lines.append(f"{'Address':<14} {'Name':<40} {'Type':<30} {'Size':>8}")
        lines.append("-" * 94)
        for v in variables:
            lines.append(
                f"0x{v.address:<12X}  {v.name:<40} {self._format_type(v.type_info):<30} {v.size:>8}"
            )
        return "\n".join(lines)

    def to_csv_string(self, variables: list[Variable]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Address", "Name", "Type", "Size"])
        for v in variables:
            writer.writerow([
                f"0x{v.address:08X}",
                v.name,
                self._format_type(v.type_info),
                v.size,
            ])
        return buf.getvalue()

    def to_json_string(self, variables: list[Variable]) -> str:
        data = []
        for v in variables:
            data.append({
                "address": f"0x{v.address:08X}",
                "name": v.name,
                "type": self._format_type(v.type_info),
                "size": v.size,
            })
        return json.dumps(data, indent=2, ensure_ascii=False)

    def to_excel(self, variables: list[Variable], filepath: str):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Variables"
        ws.append(["Address", "Name", "Type", "Size"])
        for v in variables:
            ws.append([
                f"0x{v.address:08X}",
                v.name,
                self._format_type(v.type_info),
                v.size,
            ])
        wb.save(filepath)

    @staticmethod
    def _format_type(type_info: Optional[TypeInfo]) -> str:
        if type_info is None:
            return "?"
        if isinstance(type_info, BaseType):
            return type_info.name
        if isinstance(type_info, StructType):
            prefix = "union " if type_info.is_union else "struct "
            return f"{prefix}{type_info.name}"
        if isinstance(type_info, ArrayType):
            elem = VariableTableFormatter._format_type(type_info.element_type)
            return f"{elem}[{type_info.count}]"
        if isinstance(type_info, PointerType):
            pointed = VariableTableFormatter._format_type(type_info.pointed_type) if type_info.pointed_type else "void"
            return f"{pointed}*"
        if isinstance(type_info, EnumType):
            return f"enum {type_info.name}"
        if isinstance(type_info, TypedefType):
            return type_info.name
        return "?"


class StructLayoutFormatter:
    def to_rich_table(self, rows: list[LayoutRow]):
        from rich.table import Table
        from rich.console import Console
        from rich.text import Text

        struct_name = ""
        for r in rows:
            if r.is_total and r.type_str:
                struct_name = r.type_str

        table = Table(title=f"Struct Memory Layout", border_style="cyan")
        table.add_column("Offset", style="green")
        table.add_column("Name", style="white")
        table.add_column("Type", style="yellow")
        table.add_column("Size", style="magenta", justify="right")
        table.add_column("Details", style="dim")

        for row in rows:
            if row.is_total:
                offset_text = Text("", style="bold")
                name_text = Text(row.name, style="bold cyan")
                type_text = Text(row.type_str, style="bold cyan")
                size_text = Text(str(row.size), style="bold magenta")
                details = Text("")
            elif row.is_padding:
                offset_text = Text(f"+ {row.offset}", style="dim")
                name_text = Text("<padding>", style="dim")
                type_text = Text("---", style="dim")
                size_text = Text(str(row.size), style="dim")
                details = Text("", style="dim")
            else:
                offset_text = Text(f"+ {row.offset}")
                name_text = Text(row.name)
                type_text = Text(row.type_str)
                size_text = Text(str(row.size))
                details = Text("")
                if row.bit_size:
                    details = Text(f"bit[{row.bit_offset}:{row.bit_offset + row.bit_size - 1}]", style="dim")

            table.add_row(offset_text, name_text, type_text, size_text, details)

        console = Console()
        console.print(table)

    def to_text_table(self, rows: list[LayoutRow]) -> str:
        lines = []
        lines.append(f"{'Offset':<10} {'Name':<32} {'Type':<24} {'Size':>6}  Details")
        lines.append("-" * 88)
        for row in rows:
            if row.is_total:
                lines.append(f"{'':<10} {row.name:<32} {row.type_str:<24} {row.size:>6}")
            elif row.is_padding:
                lines.append(f"+ {row.offset:<7}  {'<padding>':<32} {'---':<24} {row.size:>6}")
            else:
                details = ""
                if row.bit_size:
                    details = f"bit[{row.bit_offset}:{row.bit_offset + row.bit_size - 1}]"
                lines.append(f"+ {row.offset:<7}  {row.name:<32} {row.type_str:<24} {row.size:>6}  {details}")
        return "\n".join(lines)

    def to_csv_string(self, rows: list[LayoutRow]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Offset", "Name", "Type", "Size", "Details"])
        for row in rows:
            if row.is_total:
                writer.writerow(["", row.name, row.type_str, row.size, ""])
            elif row.is_padding:
                writer.writerow([f"+ {row.offset}", "<padding>", "---", row.size, ""])
            else:
                details = ""
                if row.bit_size:
                    details = f"bit[{row.bit_offset}:{row.bit_offset + row.bit_size - 1}]"
                writer.writerow([f"+ {row.offset}", row.name, row.type_str, row.size, details])
        return buf.getvalue()

    def to_json_string(self, rows: list[LayoutRow]) -> str:
        data = []
        for row in rows:
            data.append({
                "offset": row.offset,
                "name": row.name,
                "type": row.type_str,
                "size": row.size,
                "is_padding": row.is_padding,
                "is_total": row.is_total,
            })
        return json.dumps(data, indent=2, ensure_ascii=False)
