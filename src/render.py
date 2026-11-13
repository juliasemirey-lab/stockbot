"""Рендер дайджеста: сводка + разбивка по поставщикам + CSV."""
from __future__ import annotations

import csv
import io
from datetime import date

from calc import Line, group_by_supplier

STATUS_ORDER = ["urgent", "plan", "closed_season", "dead", "ok"]
STATUS_LABEL = {
    "urgent": "🔴 Не хватит на следующую неделю",
    "plan": "🟡 Запас ниже целевого",
    "closed_season": "⚪ Сезон закрыт — распродажа",
    "dead": "⚪ Без движения",
    "ok": "⚪ Достаточно запаса",
}


def _links_md(ln: Line) -> str:
    if not ln.links:
        return ""
    parts = []
    for code, url in ln.links.items():
        title = "WB" if code == "wb" else code.upper()
        if ln.link_kind.get(code) == "search":
            title += "?"
        parts.append(f"[{title}]({url})")
    return " · ".join(parts)


def _sku_md(ln: Line, show_links: bool = True) -> str:
    head = f"*{ln.name}*"
    if ln.color:
        head += f" · {ln.color}"
    if ln.size:
        head += f" · {ln.size}"
    out = [
        head,
        f"к началу недели {ln.stock_at_window_start:.0f} · "
        f"спрос за неделю {ln.demand_window:.0f} → *+{ln.need:.0f} шт*",
    ]
    if show_links and ln.links:
        out.append(_links_md(ln))
    if ln.note:
        out.append(f"_{ln.note}_")
    return "\n".join(out)


def _order_deadline(ws: date | None, lines: list[Line]) -> str:
    """Срок размещения заказа, чтобы товар успел к окну.

    Считается от самого медленного поставщика среди позиций к заказу:
    заказ надо разместить не позже, чем за его срок пополнения.
    """
    if not ws:
        return "*Что заказать*"
    need = [ln for ln in lines if ln.need > 0]
    if not need:
        return "*Что заказать*"
    slowest = max(ln.arrival_days for ln in need)
    from datetime import timedelta
    deadline = ws - timedelta(days=slowest)
    if deadline < date.today():
        return f"*Что заказать* · срок пополнения {slowest} дн — к окну уже не успевает"
    return f"*Что заказать* · заказ до {deadline:%d.%m} (срок {slowest} дн)"


def render_summary(lines: list[Line], today: date, warns: list[str] | None = None,
                   top_n: int = 8) -> str:
    need_lines = [ln for ln in lines if ln.need > 0]
    total = sum(ln.need for ln in need_lines)
    urgent = [ln for ln in lines if ln.status == "urgent"]
    lost = sum(ln.lost_window for ln in lines)
    matched = len([ln for ln in lines if ln.link_kind.get("wb") == "card"])
    late = [ln for ln in lines if ln.need > 0 and not ln.arrival_in_time]

    ws = lines[0].window_start if lines else None
    we = lines[0].window_end if lines else None
    span = f"{ws:%d.%m}–{we:%d.%m}" if ws and we else "следующая неделя"

    out = [f"📦 *Дайджест {today:%d.%m.%Y}* · неделя {span}"]
    out.append(f"К заказу: *{total:.0f} шт* по {len(need_lines)} позициям, "
               f"поставщиков: {len(group_by_supplier(lines))}")
    out.append(f"_Просмотрено {len(lines)} SKU, в закупку {len(need_lines)}_")
    if urgent:
        out.append(f"⚠️ Не хватит на неделю: *{len(urgent)}* поз., "
                   f"потеряем ~{lost:.0f} шт спроса")
    if late:
        out.append(f"⏳ Не успевает к началу окна: *{len(late)}* поз. "
                   f"(срок поставки дольше {lines[0].days_until_start} дн)")
    if matched:
        out.append(f"🔗 Карточки WB найдены по баркоду: {matched} из {len(lines)}")
    out.append("")

    hot = sorted([ln for ln in lines if ln.status in ("urgent", "plan")],
                 key=lambda x: (-x.demand_window, -x.need))
    if hot:
        out.append(_order_deadline(ws, lines))
        for ln in hot[:top_n]:
            out.append(_sku_md(ln))
            out.append("")
        if len(hot) > top_n:
            out.append(f"_…и ещё {len(hot) - top_n} — в разбивке по поставщикам и CSV_")
            out.append("")

    if warns:
        out.append("*⚠️ Комплектация раздельных купальников*")
        for w in warns[:5]:
            out.append(f"· {w}")
        out.append("")

    out.append("Дальше — по поставщикам, со ссылками на карточки. Полный список в CSV.")
    return "\n".join(out)


def render_suppliers(lines: list[Line], cfg: dict) -> list[str]:
    sup_cfg = cfg.get("suppliers") or {}
    groups = group_by_supplier(lines)
    msgs: list[str] = []

    for supplier in sorted(groups, key=lambda s: -sum(x.need for x in groups[s])):
        items = sorted(groups[supplier], key=lambda x: (-x.demand_window, -x.need))
        total = sum(x.need for x in items)
        info = sup_cfg.get(supplier, {})
        contact = info.get("contact")
        lt = items[0].arrival_days if items else "—"
        ws = items[0].window_start if items else None
        we = items[0].window_end if items else None

        head = [f"🏭 *{supplier}*"]
        meta = [f"к заказу *{total:.0f} шт* по {len(items)} поз."]
        if ws and we:
            meta.append(f"на {ws:%d.%m}–{we:%d.%m}")
        head.append(" · ".join(meta))
        sub = [f"срок пополнения {lt} дн"]
        if contact:
            sub.append(contact)
        head.append(" · ".join(sub))
        head.append(f"_Создать заказ: /order {supplier}_")
        head.append("")

        chunk = head[:]
        for ln in items:
            block = _sku_md(ln)
            if sum(len(x) + 1 for x in chunk) + len(block) > 3800:
                chunk.append("_…продолжение в CSV_")
                msgs.append("\n".join(chunk))
                chunk = [f"🏭 *{supplier}* (продолжение)", ""]
            chunk.append(block)
            chunk.append("")
        msgs.append("\n".join(chunk))
    return msgs


CSV_COLUMNS = [
    ("supplier", "Поставщик"), ("article", "Артикул"), ("name", "Товар"),
    ("color", "Цвет"), ("size", "Размер"), ("category", "Категория"),
    ("status", "Статус"), ("stock", "Остаток"), ("reserve", "Резерв"),
    ("available", "Доступно"), ("in_transit", "Ожидание"),
    ("stock_at_window_start", "Остаток к началу недели"),
    ("base_daily", "Продаж в день (факт)"),
    ("forecast_daily", "Продаж в день (прогноз)"),
    ("demand_window", "Спрос за неделю"), ("lost_window", "Упущено за неделю"),
    ("safety", "Страховой запас"), ("need", "Заказать"),
    ("cover_days", "Дней запаса"), ("arrival_days", "Срок поставки, дн"),
    ("barcode", "Баркод (EAN/GTIN)"),
]


def render_csv(lines: list[Line], cfg: dict | None = None,
               only_action: bool = True) -> bytes:
    """CSV для планирования закупки.

    По умолчанию — только позиции к заказу. Позиции «достаточно запаса»
    и «без движения» в закупку не входят и только раздувают файл:
    на 3000 SKU это 2950 строк шума. Полный выгруг — only_action=False.
    """
    rows = [ln for ln in lines if ln.need > 0] if only_action else lines
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow([title for _, title in CSV_COLUMNS]
               + ["Успевает к окну", "WB совпадение", "WB ссылка"])
    for ln in sorted(rows, key=lambda x: (STATUS_ORDER.index(x.status), -x.need)):
        row = []
        for attr, _ in CSV_COLUMNS:
            v = getattr(ln, attr, "")
            if isinstance(v, float):
                v = f"{v:.2f}".replace(".", ",")
            row.append(v)
        kind = ln.link_kind.get("wb", "")
        row.append("да" if ln.arrival_in_time else "нет")
        row.append("карточка" if kind == "card" else ("поиск" if kind == "search" else ""))
        row.append(ln.links.get("wb", ""))
        w.writerow(row)
    return "\ufeff".encode("utf-8") + buf.getvalue().encode("utf-8")


def explain(ln: Line, dow_vec: list[float], seas_note: str = "") -> str:
    cover = f"{ln.cover_days:.0f} дн" if ln.cover_days is not None else "—"
    kind = ln.link_kind.get("wb", "")
    ws, we = ln.window_start, ln.window_end
    rows = [
        f"*{ln.name}* · {ln.color or '—'} · {ln.size or '—'}",
        f"Артикул: `{ln.article or ln.sku_id}`",
        f"Поставщик: {ln.supplier or '—'}",
    ]
    if ln.barcodes:
        rows.append(f"Баркоды: {', '.join(ln.barcodes)}")
        if kind == "card":
            rows.append(f"Карточка WB: совпадение по `{ln.barcode_matched}` {_links_md(ln)}")
        elif kind == "search":
            rows.append(f"Карточка WB: *не найдена по баркоду*, ссылка на поиск {_links_md(ln)}")
        else:
            rows.append("Карточка WB: не найдена")
    else:
        rows.append("Баркоды: *не заполнены в МойСклад* — сопоставление невозможно")
    rows += [
        "",
        f"Окно: {ws:%d.%m}–{we:%d.%m} (через {ln.days_until_start} дн)",
        f"Остаток {ln.stock:.0f} − резерв {ln.reserve:.0f} = *доступно {ln.available:.0f}*",
        f"Ожидание (в пути): {ln.in_transit:.0f}",
        f"К началу окна останется: *{ln.stock_at_window_start:.1f}*",
        "",
        f"Продаж в день (факт): {ln.base_daily:.3f}",
    ]
    if seas_note:
        rows.append(f"Сезонная поправка: {seas_note}")
    rows += [
        f"Продаж в день (прогноз): {ln.forecast_daily:.3f}",
        f"Веса дней недели: {', '.join(f'{x:.2f}' for x in dow_vec)}",
        f"Спрос за неделю: {ln.demand_window:.1f} · упущено: {ln.lost_window:.1f}",
        "",
        f"Страховой запас (z=1.65 на 7 дн): {ln.safety:.1f}",
        f"Дней запаса: {cover}",
        f"Срок пополнения: {ln.arrival_days} дн · "
        f"{'успевает' if ln.arrival_in_time else 'НЕ успевает'} к началу окна",
        "",
        f"*Заказать: {ln.need:.0f} шт*",
        f"Статус: {STATUS_LABEL.get(ln.status, ln.status)}",
    ]
    return "\n".join(rows)
