"""Текст дайджеста и CSV.

Сообщение уходит с parse_mode=HTML, поэтому все названия экранируются:
подчёркивания и звёздочки в артикулах иначе съедали пробелы.

Тон меняется по ситуации: загруженная неделя — собранно и по делу,
тихая — спокойно. Метки срочности ставятся по числу дней запаса,
ссылки идут голым адресом без подписи [WB].
"""
from __future__ import annotations

from datetime import date, timedelta
from html import escape

from calc import Item, window_start

# метка, если остатка хватает меньше чем на столько дней
HOT = 3
SOON = 8
MAX_BLOCKS = 5


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    tail = n % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _week(start: date, horizon: int = 7) -> str:
    end = start + timedelta(days=horizon - 1)
    months = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
    if start.month == end.month:
        return f"{start.day}–{end.day} {months[end.month - 1]}"
    return (f"{start.day} {months[start.month - 1]} – "
            f"{end.day} {months[end.month - 1]}")


def _line(it: Item) -> str:
    """Строка позиции: остаток, на сколько хватит, сколько заказать."""
    head = escape(it.name)
    tail_bits = [b for b in (it.color, it.size) if b]
    if tail_bits:
        head += " · " + " · ".join(escape(b) for b in tail_bits)

    parts = []
    parts.append(f"осталось <b>{it.available:.0f}</b>")
    if it.cover_days is not None:
        d = int(it.cover_days)
        if d <= 0:
            parts.append("закончился")
        else:
            parts.append(f"хватит на {d} {_plural(d, 'день', 'дня', 'дней')}")
    else:
        parts.append("продаж не было")
    parts.append(f"→ нужно <b>{it.need:.0f}</b>")

    body = " · ".join(parts)
    if it.supplier and it.supplier != "—":
        body += f"   <i>{escape(it.supplier)}</i>"

    out = [head, body]
    if it.wb_url:
        out.append(it.wb_url)
    return "\n".join(out)


def _header(need: list[Item], start: date, horizon: int) -> list[str]:
    total = sum(i.need for i in need)
    n = len(need)
    hot = [i for i in need if i.cover_days is not None and i.cover_days <= HOT]

    out = [f"🧵 <b>Закупка на неделю {_week(start, horizon)}</b>", ""]

    if hot:
        out.append(
            f"{n} {_plural(n, 'позиция', 'позиции', 'позиций')}, "
            f"{total:.0f} шт. "
            f"{_plural(len(hot), 'Одна кончится', 'Кончатся', 'Кончатся')} "
            f"в ближайшие дни: <b>{len(hot)}</b> — "
            f"если не заказать сейчас, часть недели простоим без товара.")
    else:
        out.append(
            f"{n} {_plural(n, 'позиция', 'позиции', 'позиций')}, "
            f"{total:.0f} шт. Ничего не горит — можно заказать без спешки.")

    out.append("")
    return out


def _supplier_totals(need: list[Item]) -> list[tuple[str, float, int]]:
    acc: dict[str, list] = {}
    for it in need:
        key = it.supplier or "Без поставщика"
        rec = acc.setdefault(key, [0.0, 0])
        rec[0] += it.need
        rec[1] += 1
    rows = [(k, v[0], v[1]) for k, v in acc.items()]
    rows.sort(key=lambda x: -x[1])
    return rows


def digest(need: list[Item], today: date, top_n: int = 15,
           horizon: int = 7) -> str:
    start = window_start(today)

    if not need:
        return _empty(start, horizon)

    out = _header(need, start, horizon)

    hot = [i for i in need if i.cover_days is not None and i.cover_days <= HOT]
    soon = [i for i in need if i.cover_days is None or i.cover_days > HOT]

    shown = 0

    if hot:
        out.append("<b>🔥 Горит</b>")
        out.append("")
        for it in hot[:top_n]:
            out.append(_line(it))
            out.append("")
            shown += 1

    if soon and shown < top_n:
        out.append("<b>⏳ Дотянет до конца недели, но впритык</b>")
        out.append("")
        for it in soon[:top_n - shown]:
            out.append(_line(it))
            out.append("")
            shown += 1

    rest = len(need) - shown
    if rest > 0:
        out.append(f"<i>…и ещё {rest} — в CSV</i>")
        out.append("")

    totals = _supplier_totals(need)
    if len(totals) > 1 or totals[0][0] != "Без поставщика":
        out.append("<b>📦 По поставщикам</b>")
        out.append("")
        for name, qty, cnt in totals[:MAX_BLOCKS]:
            label = escape(name)
            out.append(f"{label} — <b>{qty:.0f}</b> шт / "
                       f"{cnt} {_plural(cnt, 'позиция', 'позиции', 'позиций')}")
        if len(totals) > MAX_BLOCKS:
            out.append(f"<i>…и ещё {len(totals) - MAX_BLOCKS} "
                       f"{_plural(len(totals) - MAX_BLOCKS, 'поставщик', 'поставщика', 'поставщиков')}"
                       f" — в CSV</i>")
        out.append("")

    out.append("<i>Полный список — в CSV, там же колонка «Поставщик».</i>")
    return "\n".join(out).rstrip()


def _empty(start: date, horizon: int) -> str:
    return "\n".join([
        f"🧵 <b>Закупка на неделю {_week(start, horizon)}</b>",
        "",
        "Заказывать нечего — запаса хватает до конца недели.",
    ])


def csv_bytes(need: list[Item]) -> bytes:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Артикул", "Товар", "Категория", "Поставщик",
                "Остаток", "Резерв", "Доступно", "В пути",
                "Продано за период", "Хватит дней", "Заказать",
                "Профиль", "Баркоды", "WB"])
    for it in need:
        w.writerow([
            it.article, it.name, it.category, it.supplier,
            f"{it.stock:.0f}", f"{it.reserve:.0f}", f"{it.available:.0f}",
            f"{it.in_transit:.0f}", f"{it.sold:.0f}",
            "" if it.cover_days is None else f"{it.cover_days:.0f}",
            f"{it.need:.0f}", it.profile,
            " ".join(it.barcodes), it.wb_url,
        ])
    return "\ufeff".encode("utf-8") + buf.getvalue().encode("utf-8")
