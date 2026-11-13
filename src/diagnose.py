"""Диагностика: отвечает на вопрос «правда ли заказывать нечего».

Показывает не «сколько записей пришло», а экономику: сколько продаём в
день, сколько лежит на складе, на сколько дней этого хватает. Если цифры
сходятся — «заказывать нечего» верно. Если нет — видно, где разрыв.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from calc import build_lines, seasonal_note
from ms_client import MS, MoySkladError
import pipeline


def run(cfg: dict) -> int:
    ms = MS()
    store = cfg.get("warehouse")
    window = int(cfg.get("velocity_window_days", 28))
    today = date.today()
    problems = 0

    print("=" * 62)
    print("ДИАГНОСТИКА: хватает ли запаса на следующую неделю")
    print("=" * 62)
    print(f"Склад: {store!r} · окно оценки спроса: {window} дн.")
    print()

    # --- 1. Сырые источники -------------------------------------------
    meta = ms.assortment_meta()
    stock = ms.stock(store)
    shipped = ms.shipped(window, store)
    returns = ms.returns(window)

    units_sold = sum(shipped.values())
    units_returned = sum(returns.values())
    units_net = units_sold - units_returned
    stock_total = sum(v.get("stock", 0) for v in stock.values())
    daily_total = units_net / window if window else 0

    # --- сырой ответ по документам: видно, есть ли позиции ------------
    try:
        docs = ms.shipped_raw(window)
    except Exception as e:  # noqa: BLE001
        print(f"  ОШИБКА запроса отгрузок: {e}")
        docs = []
    if docs:
        kind, first = docs[0]
        pos = first.get("positions")
        has_rows = isinstance(pos, dict) and isinstance(pos.get("rows"), list)
        print("── Сырой ответ по отгрузкам " + "─" * 33)
        print(f"  документов получено       {len(docs):>8}")
        print(f"  первый документ: {kind} {first.get('name', '—')}")
        print(f"  склад в документе: {(first.get('store') or {}).get('name', '— не раскрыт')}")
        print(f"  ключ positions: {'есть' if isinstance(pos, dict) else 'НЕТ'}"
              f" · rows: {len(pos.get('rows') or []) if has_rows else '—'}")
        if isinstance(pos, dict) and pos.get("meta"):
            print(f"  meta позиций: {pos['meta'].get('href', '')[-40:]}")
        print()

    print("── Источники " + "─" * 50)
    print(f"  SKU в номенклатуре      {len(meta):>8}")
    print(f"  SKU с остатком          {len(stock):>8}")
    print(f"  SKU с продажами         {len(shipped):>8}")

    has_sales = len(shipped) > 0
    if not has_sales:
        print("  ⚠ ПРОДАЖ ЗА ПЕРИОД НЕТ — расчёт будет нулевым по определению")
        problems += 1
    else:
        avg_per_sku = units_sold / max(len(shipped), 1)
        print(f"  продано штук            {units_sold:>8.0f}"
              f"  (в среднем {avg_per_sku:.1f} на проданный SKU)")
    if units_returned:
        print(f"  возвращено штук         {units_returned:>8.0f}")
    print()

    # --- 2. Экономика «в целом» ---------------------------------------
    print("── Баланс закупки " + "─" * 44)
    print(f"  продаём в день          {daily_total:>8.2f} шт")
    print(f"  лежит на складе         {stock_total:>8.0f} шт")
    if daily_total > 0.01:
        cover = stock_total / daily_total
        print(f"  хватит на               {cover:>8.0f} дн."
              f"  ({cover / 7:.1f} недель)")
        verdict = ("запаса больше чем на полгода — «нечего заказывать» правдоподобно"
                   if cover > 180 else
                   "запас сопоставим с недельным спросом — стоит посмотреть позиции")
        print(f"  → {verdict}")
    else:
        print("  хватит на               ∞  (продаж нет)")
        problems += 1
    print()

    # --- 3. Категории: совпали ли папки -------------------------------
    pipeline.UNMAPPED.clear()
    rows = pipeline.build_rows_from_ms(ms, cfg, with_links=False)
    lines = build_lines(rows, cfg, today)

    print("── Категории " + "─" * 49)
    if pipeline.UNMAPPED:
        print(f"  ⚠ НЕ СОВПАЛИ с folder_map: {len(pipeline.UNMAPPED)}")
        for f in sorted(pipeline.UNMAPPED)[:12]:
            print(f"      · {f!r}  → категория по умолчанию (blouses)")
        print("    От категории зависят сезонность, кратность и срок поставки.")
        print("    Допишите эти папки в config.yaml → folder_map.")
        problems += 1
    else:
        print("  все папки распознаны")
    cats: dict[str, int] = {}
    for ln in lines:
        cats[ln.category] = cats.get(ln.category, 0) + 1
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        title = (cfg.get("categories") or {}).get(c, {}).get("title", c)
        print(f"      {title:<32} {n:>6} SKU")
    print()

    # --- 4. Итог по заказу --------------------------------------------
    need = [ln for ln in lines if ln.need > 0]
    total_need = sum(ln.need for ln in need)
    print("── Заказ " + "─" * 53)
    print(f"  позиций к заказу        {len(need):>8} из {len(lines)}")
    print(f"  штук к заказу           {total_need:>8.0f}")
    note = seasonal_note(cfg, lines)
    if note:
        print(f"  сезонность              {note}")
    print()

    if not need and lines:
        print("  Почему ноль: смотрим 10 самых ходовых позиций")
        print(f"  {'товар':<28} {'спрос/дн':>9} {'остаток':>8} {'дней':>6} {'надо':>5}")
        for ln in sorted(lines, key=lambda x: -x.base_daily)[:10]:
            cover = f"{ln.cover_days:.0f}" if ln.cover_days is not None else "—"
            name = f"{ln.name[:22]} {ln.size}"[:28]
            print(f"  {name:<28} {ln.base_daily:>9.2f} {ln.available:>8.0f}"
                  f" {cover:>6} {ln.need:>5.0f}")
        print()
        top = sorted(lines, key=lambda x: -x.base_daily)[:5]
        if top and top[0].base_daily <= 0.01:
            print("  ⚠ у самых ходовых позиций спрос нулевой — данные о продажах не дошли")
            problems += 1
        else:
            print("  Спрос есть, но запаса хватает — заказ действительно не нужен.")

    print("=" * 62)
    if problems:
        print(f"НАЙДЕНО ПРОБЛЕМ: {problems} — см. пометки ⚠ выше")
    elif need:
        print(f"Данные согласованы. К заказу {len(need)} поз. / {total_need:.0f} шт.")
    else:
        print("Данные согласованы. Заказывать действительно нечего —")
        print("запаса хватает на весь расчётный период.")
    print("=" * 62)
    return 1 if problems else 0
