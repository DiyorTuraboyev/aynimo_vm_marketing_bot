"""Reads the weekly VM_MARKETING Excel and turns every row into Product objects.

Tolerant by design:
  * header row may not be the first row, header names may vary in case/spaces;
  * store columns are detected automatically (s01, s02 ... s10, s11 / "s01 stock");
  * warehouse columns (w1, w2 / "w1 stock") are detected automatically;
  * "-" / empty cells are treated as "no value", not as 0;
  * Department column = VM / MARKETING ("VM/MARKETING" = both departments);
  * Comment column = free-text note. A repeated department word at its start
    ("MARKETING Gamashlar ...") is removed from the note.
  * Old files without a Department column: department is read from the start of Comment.
"""
from __future__ import annotations

import math
import numbers
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEPARTMENTS

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("наименование", "name", "nomi", "nomlanishi"),
    "brand": ("бренд", "brand", "brend"),
    "category": ("категория", "category", "kategoriya"),
    "subcategory": ("подкатегория", "subcategory", "podkategoriya"),
    "model": ("модель", "model"),
    "price": ("price", "цена", "narx", "narxi"),
    "photo": ("photo_path", "photo path", "rasm", "image"),
    "stock": ("stock", "total stock", "qoldiq", "остаток", "jami qoldiq"),
    "comment": ("comment", "comments", "izoh", "комментарий", "коммент"),
    "department": ("department", "dept", "bo'lim", "bolim", "bo‘lim", "отдел", "департамент"),
}

STORE_RE = re.compile(r"^(s\d{1,3})(?:\s*stock)?$")
WAREHOUSE_RE = re.compile(r"^(w\d{1,3})(?:\s*stock)?$")

DEPT_WORDS = {
    "VM": "VM",
    "MARKETING": "MARKETING",
    "MARKETINGGA": "MARKETING",
    "MARKETINGUCHUN": "MARKETING",
    "МАРКЕТИНГ": "MARKETING",
    "ВМ": "VM",
}
DEPT_PREFIX_RE = re.compile(
    r"^\s*((?:VM|ВМ|MARKETING\w*|МАРКЕТИНГ)(?:\s*[/+&,]\s*(?:VM|ВМ|MARKETING\w*|МАРКЕТИНГ))*)\b"
    r"[\s:;,.\-–—]*(.*)$",
    re.IGNORECASE | re.DOTALL,
)

EMPTY_MARKS = {"", "-", "—", "–", "nan", "none", "null", "#n/a"}


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in EMPTY_MARKS


def clean_text(value) -> str:
    if is_empty(value):
        return ""
    text = str(value).strip()
    # Excel floats like 26.0 -> "26"
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    return text


def parse_number(value) -> float | None:
    if is_empty(value) or isinstance(value, bool):
        return None
    if isinstance(value, numbers.Real):
        number = float(value)
        return None if math.isnan(number) else number
    text = str(value).strip().replace(" ", "").replace(" ", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value) -> int | None:
    number = parse_number(value)
    return None if number is None else int(round(number))


def parse_departments(comment: str) -> tuple[list[str], str]:
    """'MARKETING Gamashlar ...' -> (['MARKETING'], 'Gamashlar ...')."""
    match = DEPT_PREFIX_RE.match(comment or "")
    if not match:
        return [], (comment or "").strip()
    departments: list[str] = []
    for word in re.split(r"[/+&,\s]+", match.group(1).upper()):
        dept = DEPT_WORDS.get(word) or ("MARKETING" if word.startswith("MARKETING") else None)
        if dept and dept not in departments:
            departments.append(dept)
    return departments, match.group(2).strip()


@dataclass
class Product:
    row: int  # Excel row number (as the user sees it)
    department: str
    note: str
    name: str
    brand: str
    category: str
    subcategory: str
    model: str
    price_raw: object
    photo_path: str
    total_stock: int | None
    stores: dict[str, int | None]  # "S01" -> qty (ordered as in Excel)
    warehouses: dict[str, int | None]  # "W1" -> qty

    @property
    def title(self) -> str:
        return self.name or self.model or f"{self.row}-qator"

    @property
    def label(self) -> str:
        return f"{self.row}-qator {self.model or self.name}".strip()

    def stock_total(self) -> int | None:
        if self.total_stock is not None:
            return self.total_stock
        values = [v for v in list(self.stores.values()) + list(self.warehouses.values()) if v is not None]
        return sum(values) if values else None


@dataclass
class LoadResult:
    source: Path
    sheet: str
    products: list[Product] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    store_codes: list[str] = field(default_factory=list)
    warehouse_codes: list[str] = field(default_factory=list)

    def for_department(self, department: str) -> list[Product]:
        return [p for p in self.products if p.department == department]

    def count(self, department: str) -> int:
        return sum(1 for p in self.products if p.department == department)

    @property
    def without_photo(self) -> int:
        return sum(1 for p in self.products if not p.photo_path)


class ExcelFormatError(ValueError):
    pass


def _read_sheets(path: Path) -> list[tuple[str, list[list]]]:
    """All sheets as lists of rows (values only, formulas -> cached values).
    Uses openpyxl directly: pandas/numpy cost too much memory on small hosting plans."""
    from openpyxl import load_workbook  # lazy: not needed in memory while the bot idles

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = []
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            width = max((len(r) for r in rows), default=0)
            sheets.append((ws.title, [r + [None] * (width - len(r)) for r in rows]))
        return sheets
    finally:
        wb.close()


def _find_header(raw: list[list]) -> tuple[int, dict[str, int], dict[str, int], dict[str, int]] | None:
    for idx in range(min(len(raw), 15)):
        cells = [_norm(v) if not is_empty(v) else "" for v in raw[idx]]
        fields: dict[str, int] = {}
        stores: dict[str, int] = {}
        warehouses: dict[str, int] = {}
        for col, cell in enumerate(cells):
            if not cell:
                continue
            for key, aliases in FIELD_ALIASES.items():
                if key not in fields and cell in aliases:
                    fields[key] = col
                    break
            else:
                if m := STORE_RE.match(cell):
                    stores.setdefault(m.group(1).upper(), col)
                elif m := WAREHOUSE_RE.match(cell):
                    warehouses.setdefault(m.group(1).upper(), col)
        if ("department" in fields or "comment" in fields) and ("model" in fields or "name" in fields):
            return idx, fields, stores, warehouses
    return None


def load_products(path: str | Path) -> LoadResult:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Fayl topilmadi: {path.name}")

    found = None
    for sheet_name, raw in _read_sheets(path):
        header = _find_header(raw)
        if header:
            found = (sheet_name, raw, header)
            break

    if not found:
        raise ExcelFormatError(
            "Excelda sarlavha topilmadi. Kamida 'Модель' (yoki 'Наименование') "
            "va 'Department' ustunlari bo'lishi kerak."
        )

    sheet_name, raw, (header_idx, fields, stores, warehouses) = found
    result = LoadResult(
        source=path,
        sheet=str(sheet_name),
        store_codes=list(stores),
        warehouse_codes=list(warehouses),
    )

    def cell(values: list, key: str):
        col = fields.get(key)
        return values[col] if col is not None and col < len(values) else None

    for idx in range(header_idx + 1, len(raw)):
        values = raw[idx]
        excel_row = idx + 1
        name = clean_text(cell(values, "name"))
        model = clean_text(cell(values, "model"))
        comment = clean_text(cell(values, "comment"))
        dept_cell = clean_text(cell(values, "department"))

        if not (name or model or comment or dept_cell):
            continue  # empty line

        ident = f"{excel_row}-qator {model or name}".strip()

        # The note is the Comment without a repeated "VM"/"MARKETING" at its start.
        comment_depts, comment_note = parse_departments(comment)
        note = comment_note if comment_depts else comment

        if dept_cell:
            departments, _ = parse_departments(dept_cell)
            if not departments:
                result.skipped.append(
                    f"{ident}: Department noto'g'ri — '{dept_cell[:30]}' (VM yoki MARKETING bo'lishi kerak)"
                )
                continue
            if comment_depts and set(comment_depts) != set(departments):
                result.warnings.append(
                    f"{ident}: Department = {'/'.join(departments)}, lekin Comment "
                    f"'{'/'.join(comment_depts)}' bilan boshlangan — Department olindi"
                )
        elif comment_depts:
            departments = comment_depts  # old template: department written in Comment
        else:
            result.skipped.append(f"{ident}: Department bo'sh")
            continue

        store_values = {code: parse_int(values[col]) for code, col in stores.items()}
        warehouse_values = {code: parse_int(values[col]) for code, col in warehouses.items()}
        photo = clean_text(cell(values, "photo")).replace("\\", "/")

        negatives = [f"{c} ({v})" for c, v in store_values.items() if v is not None and v < 0]
        if negatives:
            result.warnings.append(f"{ident}: manfiy qoldiq {', '.join(negatives)}")
        if not photo:
            result.warnings.append(f"{ident}: PHOTO_PATH yo'q — rasmsiz yuboriladi")

        for dept in departments:
            if dept not in DEPARTMENTS:
                continue
            result.products.append(
                Product(
                    row=excel_row,
                    department=dept,
                    note=note,
                    name=name,
                    brand=clean_text(cell(values, "brand")),
                    category=clean_text(cell(values, "category")),
                    subcategory=clean_text(cell(values, "subcategory")),
                    model=model,
                    price_raw=cell(values, "price"),
                    photo_path=photo,
                    total_stock=parse_int(cell(values, "stock")),
                    stores=dict(store_values),
                    warehouses=dict(warehouse_values),
                )
            )

    return result
