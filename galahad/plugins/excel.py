"""Microsoft Excel workbooks (.xlsx) via openpyxl. Writes are validated first
(range, size, formula safety); edits keep a backup; results are re-read to verify."""
from __future__ import annotations

import re
from pathlib import Path

from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.utils.cell import coordinate_from_string

from ..plugin_api import Health, Manifest, Plugin, PluginError, command
from . import _office as O

_range = range          # several commands have a parameter called `range`, which shadows the builtin
MAX_CELLS = 50_000
BAD_FORMULA = re.compile(r"(?i)(\[|https?:|file:|\\|[a-z]:/|cmd\s*\||powershell|dde|webservice|hyperlink\s*\(|importxml|importdata|\.xls[xmb]?\b|@)")


def check_formula(f: str) -> None:
    if f.count("(") != f.count(")"):
        raise PluginError(f"Formula has unbalanced parentheses: {f}")
    if BAD_FORMULA.search(f):
        raise PluginError("That formula references external data or commands, which I won't write.")
    if len(f) > 500:
        raise PluginError("Formula too long.")


def check_cell(ref: str) -> str:
    try:
        col, row = coordinate_from_string(ref.upper())
        if row < 1 or row > 1_048_576:
            raise ValueError
        return f"{col}{row}"
    except Exception:
        raise PluginError(f"'{ref}' isn't a valid cell reference (try A1).")


def check_range(rng: str) -> tuple[int, int, int, int]:
    try:
        c1, r1, c2, r2 = range_boundaries(rng.upper())
        if None in (c1, r1, c2, r2) or c2 < c1 or r2 < r1:
            raise ValueError
        if (c2 - c1 + 1) * (r2 - r1 + 1) > 200_000:
            raise PluginError("That range is too large.")
        return c1, r1, c2, r2
    except PluginError:
        raise
    except Exception:
        raise PluginError(f"'{rng}' isn't a valid range (try A1:C10).")


class Excel(Plugin):
    manifest = Manifest(
        id="excel", name="Microsoft Excel", category="Office", version="1.0.0",
        description="Create, read, write, format, calculate, chart and save Excel (.xlsx) workbooks.",
        permissions=["office.documents", "fs.write", "system.control"], requires_python=["openpyxl"],
        setup_help="Formulas are stored for Excel to calculate when the file is opened; GALAHAD computes simple sums/averages itself in 'calculate'.")

    def health(self):
        return Health("ok", "openpyxl available")

    def _wb(self, p: Path, data_only=False):
        import openpyxl
        try:
            return openpyxl.load_workbook(str(p), data_only=data_only)
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"I can't open {p.name} as an Excel workbook ({type(e).__name__}).")

    def _sheet(self, wb, name: str = ""):
        if name:
            if name not in wb.sheetnames:
                raise PluginError(f"No sheet named '{name}'. Sheets: {', '.join(wb.sheetnames)}.")
            return wb[name]
        return wb.active

    @command(description="Open a workbook in Excel.", permissions=("system.control",), params={"path": {"type": "string", "required": True}})
    def open(self, path):
        return O.open_in_app(O.resolve(path, ".xlsx", must_exist=True))

    @command(description="Create a workbook, optionally with a first sheet of rows (list of lists). Asks before replacing a file.",
             permissions=("office.documents", "fs.write"), confirm=O.overwrite_confirm("path", ".xlsx"),
             params={"path": {"type": "string", "required": True}, "sheet": {"type": "string", "default": "Sheet1"}, "rows": {"type": "array"}})
    def create(self, path, sheet="Sheet1", rows=None):
        import openpyxl
        p = O.resolve(path, ".xlsx")
        b = O.backup(p) if p.exists() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        wb.active.title = sheet[:31]
        wb.save(str(p))
        if rows:
            self._write_cells(str(p), rows, "A1", sheet[:31], False)
        O.verify_saved(p, 3000)
        return {"message": f"Saved {p}" + (f" (original backed up to {b.name})" if b else ""), "data": {"path": str(p)}}

    @command(description="Sheet names and used range for each sheet.", permissions=("office.documents",), readonly=True, params={"path": {"type": "string", "required": True}})
    def info(self, path):
        p = O.resolve(path, ".xlsx", must_exist=True)
        wb = self._wb(p)
        sheets = [{"name": ws.title, "range": ws.dimensions, "rows": ws.max_row, "columns": ws.max_column} for ws in wb.worksheets]
        return {"message": "; ".join(f"{s['name']} ({s['range']})" for s in sheets), "data": {"sheets": sheets}}

    @command(description="Read a range of values (default: the used range, up to 200 rows).", permissions=("office.documents",), readonly=True,
             params={"path": {"type": "string", "required": True}, "sheet": {"type": "string"}, "range": {"type": "string"},
                     "show_formulas": {"type": "boolean", "default": False}})
    def read_sheet(self, path, sheet="", range="", show_formulas=False):
        p = O.resolve(path, ".xlsx", must_exist=True)
        ws = self._sheet(self._wb(p, data_only=not show_formulas), sheet)
        rng = range or ws.dimensions
        c1, r1, c2, r2 = check_range(rng)
        r2 = min(r2, r1 + 199)
        rows = [[ws.cell(r, c).value for c in _range(c1, c2 + 1)] for r in _range(r1, r2 + 1)]
        return {"message": f"{ws.title}!{rng}: {len(rows)} row(s). First: {rows[0] if rows else []}", "data": {"sheet": ws.title, "range": rng, "rows": rows}}

    @command(description="Get one cell's value.", permissions=("office.documents",), readonly=True,
             params={"path": {"type": "string", "required": True}, "cell": {"type": "string", "required": True}, "sheet": {"type": "string"}})
    def get_cell(self, path, cell, sheet=""):
        ref = check_cell(cell)
        p = O.resolve(path, ".xlsx", must_exist=True)
        wb = self._wb(p)
        ws = self._sheet(wb, sheet)
        raw = ws[ref].value
        cached = self._sheet(self._wb(p, data_only=True), sheet)[ref].value
        return {"message": f"{ws.title}!{ref} = {cached if cached is not None else raw}" + (f" (formula {raw})" if isinstance(raw, str) and raw.startswith("=") else ""),
                "data": {"value": cached, "raw": raw}}

    @command(description="Write rows (list of lists) starting at a cell. Validated before writing; formulas allowed if safe; keeps a backup.",
             permissions=("office.documents", "fs.write"),
             params={"path": {"type": "string", "required": True}, "start_cell": {"type": "string", "default": "A1"}, "rows": {"type": "array", "required": True},
                     "sheet": {"type": "string"}})
    def write_range(self, path, rows, start_cell="A1", sheet=""):
        return self._write_cells(path, rows, start_cell, sheet, True)

    def _write_cells(self, path, rows, start_cell, sheet, _backup):
        ref = check_cell(start_cell)
        if not rows or not all(isinstance(r, list) for r in rows):
            raise PluginError("Give the data as a list of rows, each a list of values.")
        if sum(len(r) for r in rows) > MAX_CELLS:
            raise PluginError(f"That's more than {MAX_CELLS:,} cells at once.")
        for r in rows:
            for v in r:
                if isinstance(v, str) and v.startswith("="):
                    check_formula(v)
                elif not isinstance(v, (str, int, float, bool, type(None))):
                    raise PluginError(f"Unsupported value type: {type(v).__name__}")
        p = O.resolve(path, ".xlsx", must_exist=True)
        wb = self._wb(p)
        if sheet and sheet not in wb.sheetnames:
            wb.create_sheet(sheet[:31])
        ws = self._sheet(wb, sheet[:31] if sheet else "")
        col0, row0 = coordinate_from_string(ref)
        c0 = range_boundaries(f"{col0}1:{col0}1")[0]
        b = O.backup(p) if _backup else None
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                ws.cell(row0 + i, c0 + j).value = v
        wb.save(str(p))
        back = self._sheet(self._wb(p), sheet[:31] if sheet else ws.title)
        mism = [(i, j) for i, r in enumerate(rows) for j, v in enumerate(r) if v is not None and back.cell(row0 + i, c0 + j).value != v and not (isinstance(v, float) and back.cell(row0 + i, c0 + j).value == v)]
        return {"ok": not mism, "message": f"Wrote {sum(len(r) for r in rows)} cell(s) at {ws.title}!{ref} and re-read them to verify." if not mism else
                f"{len(mism)} cell(s) did not read back correctly.", "data": {"path": str(p), "backup": str(b) if b else None}}

    @command(description="Put a formula in a cell, e.g. =SUM(B2:B10).", permissions=("office.documents", "fs.write"),
             params={"path": {"type": "string", "required": True}, "cell": {"type": "string", "required": True}, "formula": {"type": "string", "required": True},
                     "sheet": {"type": "string"}})
    def add_formula(self, path, cell, formula, sheet=""):
        f = formula if formula.startswith("=") else "=" + formula
        check_formula(f)
        return self._write_cells(path, [[f]], cell, sheet, True)

    @command(description="Calculate sum | average | min | max | count over a numeric range (computed here, not by Excel).", permissions=("office.documents",),
             readonly=True, params={"path": {"type": "string", "required": True}, "range": {"type": "string", "required": True},
                                    "op": {"type": "string", "enum": ["sum", "average", "min", "max", "count"], "required": True}, "sheet": {"type": "string"}})
    def calculate(self, path, range, op, sheet=""):
        p = O.resolve(path, ".xlsx", must_exist=True)
        ws = self._sheet(self._wb(p, data_only=True), sheet)
        c1, r1, c2, r2 = check_range(range)
        vals = [ws.cell(r, c).value for r in _range(r1, r2 + 1) for c in _range(c1, c2 + 1)]
        nums = [float(v) for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not nums:
            raise PluginError("There are no numbers in that range (formula cells have no stored value until Excel recalculates).")
        res = {"sum": sum(nums), "average": sum(nums) / len(nums), "min": min(nums), "max": max(nums), "count": len(nums)}[op]
        return {"message": f"{op} of {ws.title}!{range} = {res:g} (from {len(nums)} numbers).", "data": {"value": res, "numbers": len(nums)}}

    @command(description="Add a worksheet.", permissions=("office.documents", "fs.write"), params={"path": {"type": "string", "required": True}, "name": {"type": "string", "required": True}})
    def add_sheet(self, path, name):
        p = O.resolve(path, ".xlsx", must_exist=True)
        wb = self._wb(p)
        if name in wb.sheetnames:
            raise PluginError(f"A sheet called '{name}' already exists.")
        if re.search(r"[\\/*?:\[\]]", name) or len(name) > 31:
            raise PluginError("Sheet names can't contain \\ / * ? : [ ] and max 31 characters.")
        O.backup(p)
        wb.create_sheet(name)
        wb.save(str(p))
        return {"ok": name in self._wb(p).sheetnames, "message": f"Added sheet '{name}'."}

    @command(description="Format a range: bold, fill colour (hex), number format, column width.", permissions=("office.documents", "fs.write"),
             params={"path": {"type": "string", "required": True}, "range": {"type": "string", "required": True}, "sheet": {"type": "string"},
                     "bold": {"type": "boolean"}, "fill": {"type": "string", "description": "hex like FFFF00"},
                     "number_format": {"type": "string", "description": "e.g. 0.00, #,##0, 0%"}, "width": {"type": "number"}})
    def format_cells(self, path, range, sheet="", bold=None, fill="", number_format="", width=None):
        from openpyxl.styles import Font, PatternFill
        c1, r1, c2, r2 = check_range(range)
        if fill and not re.fullmatch(r"[0-9A-Fa-f]{6}", fill):
            raise PluginError("Fill must be a 6-digit hex colour like FFFF00.")
        p = O.resolve(path, ".xlsx", must_exist=True)
        wb = self._wb(p)
        ws = self._sheet(wb, sheet)
        O.backup(p)
        for r in _range(r1, r2 + 1):
            for c in _range(c1, c2 + 1):
                cell = ws.cell(r, c)
                if bold is not None:
                    cell.font = Font(bold=bold)
                if fill:
                    cell.fill = PatternFill("solid", start_color=fill.upper())
                if number_format:
                    cell.number_format = number_format
        if width:
            for c in _range(c1, c2 + 1):
                ws.column_dimensions[get_column_letter(c)].width = width
        wb.save(str(p))
        chk = self._sheet(self._wb(p), sheet).cell(r1, c1)
        ok = (bold is None or bool(chk.font.bold) == bold) and (not fill or chk.fill.start_color.rgb.endswith(fill.upper())) and (not number_format or chk.number_format == number_format)
        return {"ok": ok, "message": f"Formatted {range}." if ok else "The formatting did not read back as expected."}

    @command(description="Turn a range (with a header row) into an Excel table.", permissions=("office.documents", "fs.write"),
             params={"path": {"type": "string", "required": True}, "range": {"type": "string", "required": True}, "name": {"type": "string", "required": True},
                     "sheet": {"type": "string"}})
    def create_table(self, path, range, name, sheet=""):
        from openpyxl.worksheet.table import Table, TableStyleInfo
        check_range(range)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", name):
            raise PluginError("Table names use letters, numbers and underscores, starting with a letter.")
        p = O.resolve(path, ".xlsx", must_exist=True)
        wb = self._wb(p)
        ws = self._sheet(wb, sheet)
        O.backup(p)
        t = Table(displayName=name, ref=range.upper())
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
        ws.add_table(t)
        wb.save(str(p))
        return {"ok": name in self._sheet(self._wb(p), sheet).tables, "message": f"Created table {name} on {range}."}

    @command(description="Create a chart (bar | line | pie) from a data range whose first row is the header.", permissions=("office.documents", "fs.write"),
             params={"path": {"type": "string", "required": True}, "data_range": {"type": "string", "required": True}, "chart": {"type": "string", "enum": ["bar", "line", "pie"], "default": "bar"},
                     "title": {"type": "string", "default": ""}, "anchor": {"type": "string", "default": "H2"}, "sheet": {"type": "string"}})
    def create_chart(self, path, data_range, chart="bar", title="", anchor="H2", sheet=""):
        from openpyxl.chart import BarChart, LineChart, PieChart, Reference
        c1, r1, c2, r2 = check_range(data_range)
        if c2 - c1 < 1 or r2 - r1 < 1:
            raise PluginError("A chart needs at least a label column, a value column and a header row.")
        p = O.resolve(path, ".xlsx", must_exist=True)
        wb = self._wb(p)
        ws = self._sheet(wb, sheet)
        O.backup(p)
        ch = {"bar": BarChart, "line": LineChart, "pie": PieChart}[chart]()
        ch.title = title or None
        ch.add_data(Reference(ws, min_col=c1 + 1, max_col=c2, min_row=r1, max_row=r2), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=c1, min_row=r1 + 1, max_row=r2))
        ws.add_chart(ch, check_cell(anchor))
        wb.save(str(p))
        return {"ok": len(self._sheet(self._wb(p), sheet)._charts) >= 1, "message": f"Added a {chart} chart at {anchor}."}

    @command(description="Save a copy under a new name. Asks before replacing a file.", permissions=("office.documents", "fs.write"),
             confirm=O.overwrite_confirm("dest", ".xlsx"), params={"path": {"type": "string", "required": True}, "dest": {"type": "string", "required": True}})
    def save_as(self, path, dest):
        src, dst = O.resolve(path, ".xlsx", must_exist=True), O.resolve(dest, ".xlsx")
        b = O.backup(dst) if dst.exists() else None
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._wb(src).save(str(dst))
        O.verify_saved(dst, 2000)
        return {"message": f"Saved as {dst}" + (f" (previous file backed up to {b.name})" if b else ""), "data": {"path": str(dst)}}


PLUGINS = [Excel]
