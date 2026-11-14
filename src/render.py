"""Текст дайджеста и CSV.

Структура сообщения подчинена одному правилу: в чат попадает только то, по
чему нужно действовать на этой неделе. Всё остальное уходит в CSV, но в
сообщении остаётся строка с весом файла — иначе его никто не откроет.

    🧵 Закупка на 16–22 ноября
    13 позиций срочно · 304 шт

    🔥 Горит
    [позиции с размерными сетками]

    ⬇ В файле 219 позиций, 681 шт · 47 с высокими продажами · 120 с вымытой сеткой

Числа в строке про файл — не украшение: 681 шт больше, чем в сообщении,
и это единственное, что заставляет файл открыть.

Ссылки идут голым адресом. Подпись [WB] рядом с ними в тексте не ставится.
"""
from __future__ import annotations

from datetime import date, timedelta
from html import escape

from calc import Group, window_start

MAX_CHARS = 4096          # лимит Telegram на одно сообщение
SAFE_CHARS = 3800         # оставляем запас: разбивка на два сообщения хуже обрезки


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


def _sizes_line(g: Group) -> str:
    """Размерная сетка одной строкой: остаток → заказ.

    Показываются только размеры, которые надо заказать: остальные не несут
    решения и удлиняют строку. Ярмарочный остаток идёт отдельным слагаемым
    «0+1⛺», потому что он входит в покрытие, но физически лежит не на полке.
    """
    given: dict[str, float] = {}
    for _src, qty, dst in g.moves:
        given[dst] = given.get(dst, 0.0) + float(qty)

    bits = []
    for it in g.sorted_items():
        # показываем потребность ДО подмены: это реальный дефицит размера.
        # Что снимается перемаркировкой — видно пометкой «(подмена N)» и строкой
        # ниже, а итог считается уже после подмены. Без пометки строка нечитаема:
        # сумма чисел в сетке не равна итогу и непонятно, откуда разница.
        n = it.need
        if n <= 0:
            continue
        label = escape(it.size) if it.size else "—"
        if it.yard > 0:
            bit = f"{label} {it.available:.0f}+{it.yard:.0f}⛺→{n:.0f}"
        else:
            bit = f"{label} {it.available:.0f}→{n:.0f}"
        if given.get(it.size):
            bit += f" (подмена {given[it.size]:.0f})"
        bits.append(bit)
    return " · ".join(bits)


def _group_block(g: Group) -> list[str]:
    head = escape(g.stem_display or g.stem)

    out = [head, _sizes_line(g), f"итого к заказу {g.total_order:.0f}"]

    if g.moves:
        for src, qty, dst in g.moves:
            out.append(f"перемаркировать {qty:.0f} шт: {escape(src)} → {escape(dst)}")

    if g.wb_url:
        out.append(g.wb_url)
    return out


def _supplier_blocks(groups: list[Group]) -> list[str]:
    """Поставщик заголовком, под ним его позиции. Порядок — по объёму заказа."""
    acc: dict[str, list[Group]] = {}
    for g in groups:
        key = g.supplier or "Без поставщика"
        acc.setdefault(key, []).append(g)

    blocks = sorted(acc.items(), key=lambda kv: -sum(g.total_order for g in kv[1]))
    out: list[str] = []
    for name, items in blocks:
        qty = sum(g.total_order for g in items)
        out.append(f"<b>{escape(name)}</b> — {len(items)} "
                   f"{_plural(len(items), 'позиция', 'позиции', 'позиций')} · "
                   f"{qty:.0f} шт")
        out.append("")
        for g in items:
            out += _group_block(g)
            out.append("")
    return out


def _file_line(rest: list[Group]) -> list[str]:
    """Строка про файл. Показывает вес, иначе файл не откроют."""
    if not rest:
        return []
    qty = sum(g.total_order for g in rest)
    solds = sorted(g.sold_total for g in rest)
    median = solds[len(solds) // 2] if solds else 0.0
    strong = sum(1 for g in rest if g.sold_total > median)
    # считаем по реальному дефициту — той же линейкой, что и «🔥 Горит».
    # Иначе в одном сообщении две разные метрики с одним названием.
    high = sum(1 for g in rest if g.hard_depleted_share >= 0.5)
    line = (f"⬇ <b>В файле {len(rest)} "
            f"{_plural(len(rest), 'позиция', 'позиции', 'позиций')}, {qty:.0f} шт</b>")
    bits = []
    if strong:
        bits.append(f"{strong} продаются выше среднего")
    if high:
        bits.append(f"{high} с вымытой сеткой")
    out = [line]
    if bits:
        out.append("<i>" + " · ".join(bits) + " · размеры, поставщики, ссылки</i>")
    return out


def _hot_body(shown: list[Group], total: int) -> list[str]:
    """Блок «Горит». Если показаны не все — подпись «топ N из M»."""
    head = ["<b>🔥 Горит</b>"]
    if len(shown) < total:
        head.append(f"<i>топ {len(shown)} из {total}</i>")
    return head + [""] + _supplier_blocks(shown)


def digest(hot: list[Group], rest: list[Group], today: date,
           horizon: int = 7) -> str:
    """hot — то, что горит. rest — всё остальное, уходит в файл.

    Если сообщение не влезает в лимит Telegram, горящие обрезаются по количеству
    и остаток переезжает в файл: лучше короткое сообщение, чем два.
    """
    start = window_start(today)
    head = [f"🧵 <b>Закупка на {_week(start, horizon)}</b>", ""]

    hot_qty = sum(g.total_order for g in hot)
    rest_qty = sum(g.total_order for g in rest)

    if not hot:
        if rest:
            head.append(f"Срочного нет. В файле {len(rest)} "
                        f"{_plural(len(rest), 'позиция', 'позиции', 'позиций')}, "
                        f"{rest_qty:.0f} шт — плановая докупка.")
        else:
            head.append("Заказывать нечего — запаса хватает до конца недели.")
        return "\n".join(head).rstrip()

    head.append(f"<b>{len(hot)}</b> "
                f"{_plural(len(hot), 'позиция', 'позиции', 'позиций')} срочно · "
                f"{hot_qty:.0f} шт")
    head.append("")

    body = _hot_body(hot, len(hot))
    tail = [""] + _file_line(rest)

    text = "\n".join(head + body + tail).rstrip()
    if len(text) <= SAFE_CHARS:
        return text

    # не влезло — обрезаем горящие, остаток переезжает в файл.
    # tail пересобирается на каждом шаге: в нём должно быть ВСЁ, что не влезло
    # в сообщение, иначе в тексте окажутся две разные строки про файл.
    # Остаток берём срезом hot[len(shown):] — pop() снимает с конца, поэтому
    # срез всегда точен. Сравнение по значению здесь соврало бы на похожих группах.
    shown = list(hot)
    body = _hot_body(shown, len(hot))
    tail = [""] + _file_line(rest)
    while shown and len("\n".join(head + body + tail)) > SAFE_CHARS:
        shown.pop()
        body = _hot_body(shown, len(hot))
        tail = [""] + _file_line(rest + hot[len(shown):])

    return "\n".join(head + body + tail).rstrip()


def csv_bytes(hot: list[Group], rest: list[Group]) -> bytes:
    """Полный список: и горящие, и остальное, с колонкой «Ярус»."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Ярус", "Поставщик", "Позиция", "Размер", "Артикул",
                "Остаток", "Ярмарка", "Резерв", "В пути", "Продано за период",
                "Хватит дней", "Заказать", "Баркоды", "WB"])

    def dump(groups: list[Group], tier: str) -> None:
        for g in groups:
            for it in g.sorted_items():
                n = g.after_move.get(it.size, it.need) if g.after_move else it.need
                w.writerow([
                    tier, g.supplier, g.stem_display or g.stem,
                    it.size, it.article,
                    f"{it.stock:.0f}", f"{it.yard:.0f}", f"{it.reserve:.0f}",
                    f"{it.in_transit:.0f}", f"{it.sold:.0f}",
                    "" if it.cover_days is None else f"{it.cover_days:.0f}",
                    f"{n:.0f}", " ".join(it.barcodes), it.wb_url,
                ])

    dump(hot, "горит")
    dump(rest, "в файле")
    return "\ufeff".encode("utf-8") + buf.getvalue().encode("utf-8")
