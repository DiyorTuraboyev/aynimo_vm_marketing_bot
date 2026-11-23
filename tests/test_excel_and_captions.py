from html.parser import HTMLParser

import pytest

from vm_marketing_bot.captions import (
    build_caption, department_header, preview_text, week_label, with_confirmations,
)
from vm_marketing_bot.excel import load_products, parse_departments, parse_int

ALLOWED_TAGS = {"b", "i", "code", "pre"}


def write_xlsx(path, columns: dict[str, list]) -> None:
    """Small helper: {"Header": [values...]} -> Excel file (first row = headers)."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    headers = list(columns)
    ws.append(headers)
    for i in range(max(len(v) for v in columns.values())):
        ws.append([columns[h][i] if i < len(columns[h]) else None for h in headers])
    wb.save(path)


def assert_valid_telegram_html(text: str) -> None:
    stack = []

    class Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag in ALLOWED_TAGS, f"tag <{tag}> not allowed"
            stack.append(tag)

        def handle_endtag(self, tag):
            assert stack and stack.pop() == tag, f"unbalanced </{tag}>"

    Parser().feed(text)
    assert not stack, f"unclosed tags {stack}"


@pytest.mark.parametrize("comment, departments, note", [
    ("VM", ["VM"], ""),
    ("MARKETING Gamashlar chiqishi kerak", ["MARKETING"], "Gamashlar chiqishi kerak"),
    ("marketing", ["MARKETING"], ""),
    ("VM/MARKETING: ikkalasi", ["VM", "MARKETING"], "ikkalasi"),
    ("VM SHAPKALAR UCHUN", ["VM"], "SHAPKALAR UCHUN"),
    ("salom VM", [], "salom VM"),
    ("", [], ""),
])
def test_parse_departments(comment, departments, note):
    assert parse_departments(comment) == (departments, note)


@pytest.mark.parametrize("value, expected", [
    (5, 5), (5.0, 5), ("12", 12), ("-", None), ("", None), (None, None), ("1 200", 1200), (-5, -5),
])
def test_parse_int(value, expected):
    assert parse_int(value) == expected


def test_template_loads(template):
    result = load_products(template)
    assert result.products, "template must contain products"
    assert result.count("VM") > 0
    assert result.count("MARKETING") > 0
    assert result.store_codes, "store columns (s01...) must be detected"
    for p in result.products:
        assert p.department in ("VM", "MARKETING")
        assert p.model or p.name


def test_captions_are_valid_and_fit(template):
    result = load_products(template)
    week = week_label()
    for p in result.products:
        caption = build_caption(p, week)
        assert len(caption) <= 1024
        assert_valid_telegram_html(caption)
        confirmed = with_confirmations(caption, [("Ali Valiyev", "23.11 14:20")] * 5, 1024)
        assert len(confirmed) <= 1024
        assert_valid_telegram_html(confirmed)

    for dept in ("VM", "MARKETING"):
        assert_valid_telegram_html(department_header(dept, result.for_department(dept), week))
    assert_valid_telegram_html(preview_text(result, week, {"VM": "Guruh", "MARKETING": "Guruh"}))


def test_template_department_column(template):
    result = load_products(template)
    notes = {(p.model, p.department): p.note for p in result.products}
    # "MARKETING Gamashlar ..." -> note without the repeated department word
    assert notes[("A26UUW00877", "MARKETING")] == "Gamashlar chiqishi kerak umumiy"
    # Comment that is only "VM" -> no note
    assert notes[("A26UFW00749", "VM")] == ""
    assert not result.skipped


def test_department_column_rules(tmp_path):
    path = tmp_path / "f.xlsx"
    write_xlsx(path, {
        "Модель": ["A1", "A2", "A3", "A4", "A5", "A6"],
        "Department": ["VM", "marketing", "VM/MARKETING", "", "Boshqa", "VM"],
        "Comment": ["Vitrina oldiga", "MARKETING Reklama", "", "MARKETING eski usul", "x", "MARKETING ziddiyat"],
    })
    result = load_products(path)
    got = [(p.model, p.department, p.note) for p in result.products]
    assert got == [
        ("A1", "VM", "Vitrina oldiga"),
        ("A2", "MARKETING", "Reklama"),
        ("A3", "VM", ""),
        ("A3", "MARKETING", ""),
        ("A4", "MARKETING", "eski usul"),  # empty Department -> taken from Comment
        ("A6", "VM", "ziddiyat"),  # Department wins over Comment
    ]
    assert len(result.skipped) == 1 and "A5" in result.skipped[0]
    assert any("A6" in w for w in result.warnings)


def test_region_parsing():
    from vm_marketing_bot.excel import parse_region

    r = parse_region("Buxoro, Qarshi")
    assert r.regions == ("Buxoro", "Qarshi") and not r.in_stock_only
    r = parse_region("Hamma, ostatka bor do'konlarda")
    assert r.is_all and r.in_stock_only
    assert parse_region("Hamma").is_all
    assert parse_region("toshkent").regions == ("Toshkent",)


def test_region_filters_store_table(tmp_path):
    path = tmp_path / "r.xlsx"
    write_xlsx(path, {
        "Модель": ["A1", "A2"],
        "s01": [5, 0], "s02": [3, 4], "s07": [9, 7],  # S01 Buxoro, S02 Qarshi, S07 Toshkent
        "Region": ["Buxoro, Qarshi", "Hamma, ostatka bor do'konlarda"],
        "Department": ["MARKETING", "VM"],
    })
    a1, a2 = load_products(path).products
    assert set(a1.target_stores()) == {"S01", "S02"}
    caption = build_caption(a1, "w")
    assert "📍 <b>Hudud:</b> Buxoro, Qarshi" in caption
    assert "#Buxoro" in caption and "#Qarshi" in caption
    assert "S07" not in caption
    assert "Reklama regioni" not in caption  # regions given by the Excel, no auto hint
    assert set(a2.target_stores()) == {"S02", "S07"}  # S01 has 0 -> hidden


def test_unknown_region_warns(tmp_path):
    path = tmp_path / "u.xlsx"
    write_xlsx(path, {"Модель": ["A1"], "s01": [1], "Region": ["Andijon"], "Department": ["VM"]})
    result = load_products(path)
    assert result.products and any("Andijon" in w for w in result.warnings)


def test_missing_department_and_comment_columns(tmp_path):
    path = tmp_path / "bad.xlsx"
    write_xlsx(path, {"Модель": ["X1"], "PRICE": [100]})
    with pytest.raises(ValueError):
        load_products(path)


def test_rows_without_department_are_skipped(tmp_path):
    path = tmp_path / "f.xlsx"
    write_xlsx(path, {
        "Модель": ["A1", "A2", "A3"],
        "s01": [1, 0, "-"],
        "Comment": ["VM", "", "nimadir"],
    })
    result = load_products(path)
    assert [p.model for p in result.products] == ["A1"]
    assert len(result.skipped) == 2
