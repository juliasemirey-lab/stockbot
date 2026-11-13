"""Рендер дайджеста: короткая сводка + список «что заказать» + CSV."""
from __future__ import annotations

import csv
import io
from datetime import date

from calc import Line, group_by_supplier, seasonal_note


STATUS_ORDER = ["urgent", "plan", "closed_season", "dead", "ok"]


def _links_md(ln: Line) -> str:
    url = ln.links.get("wb")
    if not url:
        return ""
    title = "WB" if ln.link_kind.get("wb") == "card" else "WB (поиск)"
    return f"[{title}]({url})"


def _sku_md(ln: Line) -> str:
    """Одна позиция: что, сколько есть, сколько заказать. И ничего лишнего."""
    head = f"*{ln.name}*"
    if ln.color:
        head += f" · {ln.color}"
    if ln.size:
        head += f" · {ln.size}"
    body = f"остаток {ln.available:.0f} · *заказать {ln.need:.0f} шт*"
    link = _links_md(ln)
    if link:
        body += f"  {link}"
    return head + "\n" + body


def _decl(n: int) -> str:
    """Склонение: 1 поставщик, 2 поставщика, 5 поставщиков."""
    if 11 <= n % 100 <= 14:
        return "ов"
    tail = n % 10
    if tail == 1:
        return ""
    if 2 <= tail <= 4:
        return "а"
    return "ов"


def render_summary(lines: list[Line], today: date, warns: list[str] | None = None,
                   cfg: dict | None = None, top_n: int = 10) -> str:
    """Только шапка: период, сезон одной фразой, итоговые цифры.

    Списка позиций здесь нет — он идёт следующим сообщением, разбитый по
    поставщикам. Иначе одни и те же строки приходят дважды.
    """
    cfg = cfg or {}
    need_lines = [ln for ln in lines if ln.need > 0]
    total = sum(ln.need for ln in need_lines)
    suppliers = len(group_by_supplier(lines))

    ws = lines[0].window_start if lines else None
    we = lines[0].window_end if lines else None
    span = f"{ws:%d.%m}–{we:%d.%m}" if ws and we else ""

    out = [f"📦 *Закупка на неделю {span}*"]
    note = seasonal_note(cfg, lines)
    if note:
        out.append(f"_{note}_")
    out.append("")

    if not need_lines:
        out.append("Ничего заказывать не нужно.")
        # Пустой заказ — это либо правда, либо данные не пришли.
        # Молча показывать «всё хорошо» опаснее, чем предупредить.
        no_sales = [ln for ln in lines if ln.base_daily <= 0]
        if lines and len(no_sales) == len(lines):
            out.append("")
            out.append("⚠️ *Продаж за период не найдено ни у одной позиции.*")
            out.append("Проверьте: есть ли отгрузки за последние "
                       f"{cfg.get('velocity_window_days', 28)} дн. и тот ли склад "
                       f"выбран (`warehouse: {cfg.get('warehouse')}`).")
            out.append("Диагностика: `python main.py doctor`")
        return "\n".join(out)

    out.append(f"Заказать: *{total:.0f} шт* по *{len(need_lines)}* позициям · "
               f"{suppliers} поставщик{_decl(suppliers)}")

    if warns:
        out.append("")
        out.append("⚠️ Комплектация купальников:")
        for w in warns[:3]:
            out.append(f"· {w}")

    return "\n".join(out)


def render_suppliers(lines: list[Line], cfg: dict) -> list[str]:
    """Отдельное сообщение на поставщика: имя, сколько, список позиций."""
    sup_cfg = cfg.get("suppliers") or {}
    groups = group_by_supplier(lines)
    msgs: list[str] = []

    for supplier in sorted(groups, key=lambda s: -sum(x.need for x in groups[s])):
        items = sorted(groups[supplier], key=lambda x: -x.need)
        total = sum(x.need for x in items)
        contact = (sup_cfg.get(supplier) or {}).get("contact")

        head = [f"🏭 *{supplier}* · заказать *{total:.0f} шт* / {len(items)} поз."]
        if contact:
            head.append(f"_{contact}_")
        head.append("")

        chunk = head[:]
        for ln in items:
            block = _sku_md(ln)
            if sum(len(x) + 1 for x in chunk) + len(block) > 3800:
                msgs.append("\n".join(chunk))
                chunk = [f"🏭 *{supplier}* (продолжение)", ""]
            chunk.append(block)
            chunk.append("")
        msgs.append("\n".join(chunk).rstrip())
    return msgs


CSV_COLUMNS = [
    ("supplier", "Поставщик"), ("article", "Артикул"), ("name", "Товар"),
    ("color", "Цвет"), ("size", "Размер"),
    ("available", "Остаток"), ("need", "Заказать"),
]


def render_csv(lines: list[Line], cfg: dict | None = None,
               only_action: bool = True) -> bytes:
    """CSV закупки: только то, что нужно заказать. Без внутрянки."""
    rows = [ln for ln in lines if ln.need > 0] if only_action else lines
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow([title for _, title in CSV_COLUMNS] + ["WB ссылка"])
    for ln in sorted(rows, key=lambda x: (x.supplier, -x.need)):
        row = []
        for attr, _ in CSV_COLUMNS:
            v = getattr(ln, attr, "")
            if isinstance(v, float):
                v = f"{v:.0f}" if attr in ("available", "need") else f"{v:.2f}".replace(".", ",")
            row.append(v)
        row.append(ln.links.get("wb", ""))
        w.writerow(row)
    return "\ufeff".encode("utf-8") + buf.getvalue().encode("utf-8")


def explain(ln: Line, dow_vec: list[float], seas_note: str = "") -> str:
    """Расшифровка по одной позиции — для /why. Здесь внутрянка уместна."""
    rows = [
        f"*{ln.name}* · {ln.color or '—'} · {ln.size or '—'}",
        f"Артикул: `{ln.article or ln.sku_id}`",
        f"Поставщик: {ln.supplier or '—'}",
        "",
        f"Остаток: {ln.stock:.0f} − резерв {ln.reserve:.0f} = *{ln.available:.0f}*",
        f"Ожидание: {ln.in_transit:.0f}",
        f"К началу окна останется: {ln.stock_at_window_start:.1f}",
        "",
        f"Продаж в день (факт): {ln.base_daily:.3f}",
    ]
    if seas_note:
        rows.append(f"Сезон: {seas_note}")
    rows += [
        f"Продаж в день (прогноз): {ln.forecast_daily:.3f}",
        f"Спрос за неделю: {ln.demand_window:.1f}",
        f"Страховой запас: {ln.safety:.1f}",
        f"Срок поставки: {ln.arrival_days} дн",
        "",
        f"*Заказать: {ln.need:.0f} шт*",
    ]
    return "\n".join(rows)
