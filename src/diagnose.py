"""Диагностика источников данных: что реально вернул МойСклад.

Нужна, когда дайджест приходит пустым. Показывает по каждому источнику
количество записей и ошибку, если запрос не удался — вместо того чтобы
молча проглотить её и показать ноль.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ms_client import MS, MoySkladError

log = logging.getLogger("stockbot")


def _try(label: str, fn, *a, **kw):
    """Выполняет запрос, возвращает (кол-во, ошибка-или-None)."""
    try:
        res = fn(*a, **kw)
        n = len(res) if hasattr(res, "__len__") else (1 if res else 0)
        return n, None
    except MoySkladError as e:
        return 0, str(e)
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def run(cfg: dict) -> int:
    """Возвращает код выхода: 0 — всё в порядке, 1 — есть проблемы."""
    ms = MS()
    store = cfg.get("warehouse")
    window = int(cfg.get("velocity_window_days", 28))
    problems = 0

    print("=" * 60)
    print("ДИАГНОСТИКА ИСТОЧНИКОВ ДАННЫХ")
    print("=" * 60)
    print(f"Склад из конфига: {store!r}")
    print(f"Окно оценки спроса: {window} дн.")
    print()

    # 1. Склады
    n, err = _try("stores", lambda: ms.stores())
    print(f"Склады в аккаунте:      {n:>6}")
    if err:
        print(f"   ОШИБКА: {err}")
        problems += 1
    else:
        for s in ms.stores()[:10]:
            mark = " ← выбран" if (s.get("name") or "").lower() == (store or "").lower() else ""
            print(f"   · {s.get('name')}{mark}")
        if store:
            try:
                ms.resolve_store(store)
            except MoySkladError as e:
                print(f"   ПРОБЛЕМА: {e}")
                problems += 1
    print()

    # 2. Номенклатура
    meta, err = {}, None
    try:
        meta = ms.assortment_meta()
    except Exception as e:  # noqa: BLE001
        err = str(e)
    print(f"Товаров и модификаций:  {len(meta):>6}")
    if err:
        print(f"   ОШИБКА: {err}")
        problems += 1
    bars = sum(1 for v in meta.values() if v.get("barcodes"))
    print(f"   с баркодами:         {bars:>6}"
          f"  ({bars * 100 // max(len(meta), 1)}%)")
    print()

    # 3. Остатки
    stock, err = {}, None
    try:
        stock = ms.stock(store)
    except Exception as e:  # noqa: BLE001
        err = str(e)
    print(f"Позиций с остатком:     {len(stock):>6}")
    if err:
        print(f"   ОШИБКА: {err}")
        problems += 1
    else:
        with_res = sum(1 for v in stock.values() if (v.get("reserve") or 0) > 0)
        print(f"   с резервом:          {with_res:>6}")
        if with_res == 0:
            print("   ВНИМАНИЕ: резерв нулевой у всех — проверьте склад")
    print()

    # 4. Отгрузки — ключевой источник скорости спроса
    shipped, err = {}, None
    try:
        shipped = ms.shipped(window, store)
    except Exception as e:  # noqa: BLE001
        err = str(e)
    print(f"Позиций с отгрузками:   {len(shipped):>6}  (за {window} дн.)")
    if err:
        print(f"   ОШИБКА: {err}")
        problems += 1
    total_units = sum(shipped.values())
    print(f"   всего штук:          {total_units:>6.0f}")
    if len(shipped) == 0:
        print("   ПРОБЛЕМА: отгрузок нет — скорость спроса будет нулевой,")
        print("             и в заказ не попадёт ни одна позиция")
        print("             проверьте: есть ли продажи за период, тот ли склад")
        problems += 1
    print()

    # 5. Возвраты
    n, err = _try("returns", lambda: ms.returns(window))
    print(f"Возвратов:              {n:>6}")
    if err:
        print(f"   ОШИБКА: {err}")
    print()

    # 6. Поступления
    now = datetime.now(timezone.utc)
    arr, _ = {}, {}
    try:
        arr, _ = ms.arrivals_with_eta(now, store)
    except Exception as e:  # noqa: BLE001
        print(f"Поступления: ОШИБКА {e}")
    print(f"Позиций в пути:         {len(arr):>6}")
    print()

    # 7. Поставщики
    sup, err = {}, None
    try:
        sup = ms.supplier_map(cfg.get("supplier_attribute") or "Поставщик")
    except Exception as e:  # noqa: BLE001
        err = str(e)
    print(f"Позиций с поставщиком:  {len(sup):>6}")
    if err:
        print(f"   ОШИБКА: {err}")
    print()

    print("=" * 60)
    if problems:
        print(f"НАЙДЕНО ПРОБЛЕМ: {problems}")
    else:
        print("Все источники отвечают. Если заказ всё равно пустой —")
        print("проверьте, что остатков действительно не хватает.")
    print("=" * 60)
    return 1 if problems else 0
