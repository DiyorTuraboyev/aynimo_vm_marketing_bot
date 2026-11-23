"""Telegram texts (HTML) for VM / Marketing posts."""
from __future__ import annotations

import re
from datetime import date
from html import escape as _html_escape

from .config import DEPARTMENT_TITLES, REGION_MIN_STOCK, now
from .excel import LoadResult, Product, parse_number

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def escape(text) -> str:
    return _html_escape(str(text), quote=False)


def week_label(day: date | None = None) -> str:
    day = day or now().date()
    return f"{day.isocalendar().week}-hafta · {day:%d.%m.%Y}"


def fmt_int(value: int | float) -> str:
    return f"{int(value):,}".replace(",", " ")


def fmt_price(raw) -> str:
    number = parse_number(raw)
    if number is None:
        text = "" if raw is None else str(raw).strip()
        return escape(text) if text and text.lower() != "nan" else "—"
    return f"{fmt_int(number)} so'm"


def fmt_qty(value: int | None) -> str:
    return "—" if value is None else str(value)


def hashtag(text: str) -> str:
    tag = re.sub(r"[^\w]+", "_", text or "", flags=re.UNICODE).strip("_")
    if not tag or tag.isdigit():
        return ""
    return "#" + tag


def store_table(product: Product) -> str:
    stores = product.target_stores()
    cells = [(code, stores[code]) for code in stores]
    cells += [(code, product.warehouses[code]) for code in product.warehouses]
    if not cells:
        return ""
    half = (len(cells) + 1) // 2
    left, right = cells[:half], cells[half:]
    lines = []
    for i, (code, value) in enumerate(left):
        line = f"{code:<4}{fmt_qty(value):>5}"
        if i < len(right):
            r_code, r_value = right[i]
            line += f"   │ {r_code:<4}{fmt_qty(r_value):>5}"
        lines.append(line)
    return "<pre>" + escape("\n".join(lines)) + "</pre>"


def _top_stores(product: Product, limit: int = 3) -> str:
    ranked = sorted(
        ((c, v) for c, v in product.target_stores().items() if v is not None and v > 0),
        key=lambda item: item[1],
        reverse=True,
    )[:limit]
    if not ranked:
        return ""
    return "🔥 Eng ko'p qoldiq: " + " · ".join(f"{c} ({v})" for c, v in ranked)


def _stock_alerts(product: Product) -> list[str]:
    stores = product.target_stores()
    empty = [c for c, v in stores.items() if v == 0]
    negative = [f"{c} ({v})" for c, v in stores.items() if v is not None and v < 0]
    lines = []
    if empty:
        lines.append("⚠️ Qoldiq yo'q: " + ", ".join(empty))
    if negative:
        lines.append("❗ Manfiy qoldiq: " + ", ".join(negative))
    return lines


def build_caption(product: Product, week: str, limit: int = CAPTION_LIMIT) -> str:
    icon, title = DEPARTMENT_TITLES[product.department]

    head = [f"{icon} <b>{title}</b> · {escape(week)}"]
    region = product.region
    if region.suggested:
        if region.totals:
            places = " · ".join(f"{escape(r)} ({q})" for r, q in region.totals)
            head.append(f"📍 <b>Hudud (bot tavsiyasi): {places}</b>")
        else:
            head.append(
                f"📍 <b>Hudud (bot tavsiyasi): yo'q — hech bir hududda qoldiq {REGION_MIN_STOCK} tadan ko'p emas</b>"
            )
    elif region.text:
        head.append(f"📍 <b>Hudud: {escape(region.text)}</b>")
    # Every post has the comment line; empty when the Excel has no comment for this product.
    head.append(f"💬 <b>Izoh:</b> <i>{escape(product.note)}</i>" if product.note else "💬 <b>Izoh:</b>")

    info = [f"🏷 <b>{escape(product.title)}</b>"]
    if product.brand:
        info.append(f"⭐ Brend: {escape(product.brand)}")
    path = " › ".join(escape(x) for x in (product.category, product.subcategory) if x)
    if path:
        info.append(f"🗂 {path}")
    if product.model and product.model != product.name:
        info.append(f"🔖 Model: <code>{escape(product.model)}</code>")
    info.append(f"💰 Narxi: {fmt_price(product.price_raw)}")

    total = product.stock_total()
    stock_line = f"📦 Jami qoldiq: <b>{fmt_qty(total)}</b> ta"
    if product.region.regions:
        in_region = [v for v in product.target_stores().values() if v is not None]
        if in_region:
            stock_line += f" · hududda: <b>{sum(in_region)}</b> ta"
    stock = [stock_line]
    table = store_table(product)

    extra = _stock_alerts(product)
    if product.department == "MARKETING":
        top = _top_stores(product)
        extra = ([top] if top else []) + extra

    tags = " ".join(
        t for t in (
            f"#{product.department}",
            *(hashtag(r) for r in product.region.regions),
            hashtag(product.subcategory),
            hashtag(product.model) if product.model != product.name else "",
        ) if t
    )

    def assemble(with_table: bool, with_extra: bool) -> str:
        blocks = ["\n".join(head), "\n".join(info)]
        stock_block = list(stock)
        if with_table and table:
            stock_block.append(table)
        if with_extra:
            stock_block.extend(extra)
        blocks.append("\n".join(stock_block))
        blocks.append(tags)
        return "\n\n".join(b for b in blocks if b)

    for with_table, with_extra in ((True, True), (False, True), (False, False)):
        caption = assemble(with_table, with_extra)
        if len(caption) <= limit:
            return caption
    # Extremely long names/notes only: drop the HTML tags so cutting is safe.
    plain = re.sub(r"<[^>]+>", "", caption)
    cut = plain[: limit - 1]
    cut = cut[: cut.rfind("&")] if cut.rfind("&") > cut.rfind(";") else cut  # no half entity
    return cut + "…"


def with_confirmations(caption: str, people: list[tuple[str, str]], limit: int) -> str:
    """Append '✅ Bajardi: ...' block to an already-built caption."""
    if not people:
        return caption
    names = ", ".join(f"{escape(name)} ({when})" for name, when in people)
    block = f"\n\n✅ <b>Bajardi:</b> {names}"
    if len(caption) + len(block) > limit:
        block = f"\n\n✅ <b>Bajardi:</b> {len(people)} kishi"
    if len(caption) + len(block) > limit:
        return caption
    return caption + block


def department_header(department: str, products: list[Product], week: str) -> str:
    icon, title = DEPARTMENT_TITLES[department]
    # Short separator only — comments are shown on each product post, not listed here.
    return "\n".join([
        f"{icon} <b>{title} — haftalik mahsulotlar</b>",
        f"📅 {escape(week)}",
        f"🧾 Jami: <b>{len(products)}</b> ta mahsulot",
    ])


def preview_text(result: LoadResult, week: str, targets: dict[str, str]) -> str:
    lines = [
        "📄 <b>Fayl tekshirildi</b>",
        f"Fayl: <code>{escape(result.source.name)}</code> (varaq: {escape(result.sheet)})",
        f"📅 {escape(week)}",
        "",
    ]
    for dept in ("VM", "MARKETING"):
        icon, title = DEPARTMENT_TITLES[dept]
        lines.append(f"{icon} {title}: <b>{result.count(dept)}</b> ta → {escape(targets[dept])}")
    stores = ", ".join(result.store_codes + result.warehouse_codes) or "topilmadi"
    lines.append(f"🏬 Do'konlar: {escape(stores)}")
    if result.without_photo:
        lines.append(f"🖼 Rasmsiz: {result.without_photo} ta (matn sifatida yuboriladi)")

    def section(title: str, items: list[str], limit: int = 10) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"{title} ({len(items)}):")
        lines.extend(f"• {escape(i)}" for i in items[:limit])
        if len(items) > limit:
            lines.append(f"… yana {len(items) - limit} ta")

    section("❌ <b>O'tkazib yuborildi</b>", result.skipped)
    section("⚠️ <b>Ogohlantirish</b>", result.warnings)

    lines.append("")
    lines.append("Quyidagi tugmalardan birini tanlang 👇")
    text = "\n".join(lines)
    if len(text) > TEXT_LIMIT:
        text = text[: text.rfind("\n", 0, TEXT_LIMIT - 10)] + "\n…"
    return text
