"""Проверки расчёта на выгрузках клиента и на синтетике.

Запуск:  python3 tests/test_calc.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from calc import (Group, Item, apply_substitution, build,  # noqa: E402
                  make_groups, net_outflow, split_size)

FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}  {detail}")


# ---------------------------------------------------------------- разбор имён

def test_sizes() -> None:
    print("\nразбор размеров и мусора")
    cases = [
        ("Дубленка двухсторон_корич 50", "Дубленка двухсторон_корич", "50"),
        ("3759-72 Дубленка 56 (58RUS)", "3759-72 Дубленка", "56"),
        ("F608 Куртка-пальто M (48-50)", "F608 Куртка-пальто", "M"),
        ("2312 фиолетовый 90G", "2312 фиолетовый", "90G"),
        ("87459 Куп слит листья ONE SIZE", "87459 Куп слит листья", "ONESIZE"),
    ]
    for raw, want_stem, want_size in cases:
        stem, size = split_size(raw)
        check(f"«{raw[:32]}» → {want_size}",
              size == want_size and stem.startswith(want_stem[:14]),
              f"получили stem={stem!r} size={size!r}")

    # мусорные пометки не должны ломать группировку
    a, _ = split_size("25375 дубленка барашек 66")
    b, _ = split_size("25375 дубленка барашек 66 (ДОБАВИТЬ БАРКОД)")
    check("пометка «ДОБАВИТЬ БАРКОД» не рвёт группу", a == b, f"{a!r} != {b!r}")

    check("пустое имя не падает", split_size("") == ("", ""))


# ---------------------------------------------------------------- подмена размеров

def make_item(size: str, available: float, demand: float,
              safety: float = 0.0) -> Item:
    need = max(demand + safety - available, 0.0)
    return Item(sku_id=size, name=f"x {size}", stem="x", size=size,
                available=available, demand=demand, safety=safety,
                need=float(round(need)))


def test_substitution() -> None:
    print("\nподмена размеров")

    # 46 в дефиците, 48 с излишком — перемаркировка должна закрыть дефицит
    g = Group(stem="x", supplier="s", supplier_id="1", sizes=[
        make_item("46", available=0, demand=3),
        make_item("48", available=8, demand=1),
    ])
    apply_substitution([g])
    check("дефицит закрыт соседом", g.after_move.get("46") == 0,
          f"after={g.after_move}")
    check("подмена записана", len(g.moves) == 1, f"moves={g.moves}")
    check("итог уменьшился", g.total_order < sum(s.need for s in g.sizes),
          f"total={g.total_order}")

    # донор сам в дефиците — отдавать не может
    g2 = Group(stem="y", supplier="s", supplier_id="1", sizes=[
        make_item("46", available=0, demand=5),
        make_item("48", available=0, demand=4),
    ])
    apply_substitution([g2])
    check("дефицитный донор не отдаёт", g2.moves == [], f"moves={g2.moves}")

    # два дефицитных размера и ОДИН донор: излишка должно хватить один раз
    g3 = Group(stem="z", supplier="s", supplier_id="1", sizes=[
        make_item("44", available=0, demand=3),
        make_item("46", available=0, demand=3),
        make_item("48", available=5, demand=0),   # излишек = 5
    ])
    apply_substitution([g3])
    given = sum(q for _, q, _ in g3.moves)
    check("донор не отдаёт больше излишка", given <= 5.0,
          f"отдал {given} при излишке 5")

    # буквенные с числовыми не смешиваются
    g4 = Group(stem="w", supplier="s", supplier_id="1", sizes=[
        make_item("M", available=0, demand=3),
        make_item("48", available=10, demand=0),
    ])
    apply_substitution([g4])
    check("буквенный не берёт у числового", g4.moves == [], f"moves={g4.moves}")

    # несоседний размер не донор
    g5 = Group(stem="v", supplier="s", supplier_id="1", sizes=[
        make_item("44", available=0, demand=3),
        make_item("46", available=0, demand=3),
        make_item("52", available=20, demand=0),
    ])
    apply_substitution([g5])
    check("несоседний размер не донор", g5.moves == [], f"moves={g5.moves}")

    # перемаркировка только целыми штуками
    g6 = Group(stem="u", supplier="s", supplier_id="1", sizes=[
        make_item("60", available=0, demand=0.4),
        make_item("62", available=0, demand=3.7),
        make_item("64", available=9, demand=0),
    ])
    apply_substitution([g6])
    check("дробная перемаркировка не появляется",
          all(q == int(q) and q >= 1 for _, q, _ in g6.moves),
          f"moves={g6.moves}")


# ---------------------------------------------------------------- группировка

def test_grouping() -> None:
    print("\nгруппировка: основа (цвет внутри) и поставщик — часть ключа")

    def it(stem, sup, sup_id, size="50"):
        return Item(sku_id=f"{stem}{sup}{size}", name=f"{stem} {size}",
                    stem=stem, stem_display=stem, size=size,
                    supplier=sup, supplier_id=sup_id, need=2.0, available=0,
                    demand=2.0)

    groups = make_groups([
        it("Дубленка чёрный", "ИП А", "1"),
        it("Дубленка чёрный", "ИП А", "1", size="52"),
        it("Дубленка корич", "ИП А", "1"),          # цвет — часть основы, другая позиция
        it("Дубленка чёрный", "ООО Б", "2"),        # другой поставщик
    ])
    check("цвет внутри основы разделяет позиции",
          len([g for g in groups if "чёрный" in g.stem]) == 2)
    check("поставщик разделяет позиции", len(groups) == 3, f"групп {len(groups)}")

    g = next(g for g in groups if "чёрный" in g.stem and g.supplier == "ИП А")
    check("размеры одной позиции собраны", len(g.sizes) == 2, f"{len(g.sizes)}")


# ---------------------------------------------------------------- расчёт заказа

def test_build() -> None:
    print("\nрасчёт потребности")

    items = {
        "a": {"name": "Тест дубленка чёрный 50", "article": "1",
              "folder": "", "barcodes": [], "supplier_id": ""},
        "b": {"name": "Тест дубленка чёрный 52", "article": "1",
              "folder": "", "barcodes": [], "supplier_id": ""},
    }
    stock = {"a": {"stock": 0, "reserve": 0, "in_transit": 0},
             "b": {"stock": 10, "reserve": 0, "in_transit": 0}}
    # продано 28 шт за 28 дней → 1 шт/день → 7 шт на неделю
    sales = {"a": 28.0, "b": 28.0}
    cfg = {"horizon_days": 7, "velocity_window_days": 28, "service_z": 1.65,
           "profiles": {}, "season_map": []}

    out = build(items, stock, sales, {}, cfg, date(2026, 11, 14))
    by = {i.size: i for i in out}

    check("дефицитный размер заказывается", by["50"].need > 0, f"need={by['50'].need}")
    check("размер с запасом не заказывается", by["52"].need == 0,
          f"need={by['52'].need}")

    # страховой запас не отрицательный и не абсурдный
    check("страховка в разумных пределах", 0 < by["50"].safety < by["50"].demand,
          f"safety={by['50'].safety:.2f} demand={by['50'].demand:.2f}")

    # ярмарка входит в покрытие
    stock2 = {"a": {"stock": 0, "reserve": 0, "in_transit": 0}}
    yard = {"a": {"stock": 20, "reserve": 0, "in_transit": 0}}
    out2 = build({"a": items["a"]}, stock2, {"a": 28.0}, {}, cfg,
                 date(2026, 11, 14), yard=yard)
    check("ярмарка закрывает потребность", out2[0].need == 0,
          f"need={out2[0].need}")
    check("ярмарка не попадает в показываемый остаток",
          out2[0].available == 0, f"available={out2[0].available}")
    check("ярмарка видна отдельно", out2[0].yard == 20, f"yard={out2[0].yard}")


# ---------------------------------------------------------------- ярусы

def test_tiers() -> None:
    print("\nярусы: горит / файл")
    import calc

    def grp(stem, sizes, sold, order, hard=None):
        """hard — дефицит без страхового запаса. По умолчанию совпадает с заказом:
        страховка на тестовых числах мала и на классификацию не влияет."""
        g = Group(stem=stem, supplier="s", supplier_id="1")
        hard = order if hard is None else hard
        for s in range(sizes):
            it = Item(sku_id=f"{stem}{s}", name=f"{stem} {44 + s * 2}",
                      stem=stem, size=str(44 + s * 2), need=order / sizes,
                      sold=sold / sizes, available=0, demand=1,
                      hard=hard / sizes)
            g.sizes.append(it)
        g.after_move = {str(44 + s * 2): order / sizes for s in range(sizes)}
        return g

    # когорта специально разная по продажам, иначе манжета «выше квартиля»
    # пропускает всех: 75-й процентиль четырёх значений лежит между двумя
    # верхними, и горячая в него не попадает
    groups = [
        grp("горячая", 6, sold=300, order=47),   # и продажи, и вымытость, и партия
        grp("средняя", 6, sold=25, order=15),    # мало продаж
        grp("мелкая", 6, sold=200, order=4),     # вымыта, но партия не стоит поездки
        grp("одиночка", 1, sold=400, order=30),  # одна позиция — не сетка
        grp("мёртвая", 6, sold=1, order=12),     # продаж нет
    ]
    cfg = {"hot_size_min": 3, "hot_share": 0.70,
           "hot_quantile": 0.75, "hot_min_qty": 12}
    hot, rest = calc.split_tiers(groups, cfg)

    names = [g.stem for g in hot]
    check("ходовая с вымытой сеткой горит", "горячая" in names, f"hot={names}")
    check("малые продажи не горят", "средняя" not in names, f"hot={names}")
    check("мелкий заказ не горит", "мелкая" not in names, f"hot={names}")
    check("одиночная позиция не горит", "одиночка" not in names, f"hot={names}")
    check("остальные ушли в файл", len(rest) == 4, f"rest={len(rest)}")

    # позиция, где заказ целиком из страхового запаса: товар ещё есть, подушка просела.
    # Такая не горит — это плановая докупка.
    cushion = [grp("подушка", 6, sold=300, order=40, hard=0)]
    hot2, rest2 = calc.split_tiers(cushion, cfg)
    check("запас есть, просела только подушка — не горит",
          "подушка" not in [g.stem for g in hot2], f"hot={[g.stem for g in hot2]}")
    check("но в файл попала", len(rest2) == 1, f"rest={len(rest2)}")


def test_sizes_module() -> None:
    print("\nразбор названий: основа = название без размера, цвет остаётся внутри")
    # Реальные названия из номенклатуры. Цвет НЕ вырезается: он часть позиции,
    # отдельная модификация — только размер. Если разбор сломается, размерная
    # сетка распадётся и позиция выпадет из «горит».
    cases = [
        # (название, ожидаемая основа, ожидаемый размер)
        ("3759-72 Дубленка двухсторон_корич 48",
         "3759-72 Дубленка двухсторон_корич", "48"),
        ("3759-72 Дубленка двухсторон_графит 50",
         "3759-72 Дубленка двухсторон_графит", "50"),
        ("25375 дубленка барашек серо-коричневый великан 66",
         "25375 дубленка барашек серо-коричневый великан", "66"),
        ("30621 Дубленка Леди Зима Длинная шоколад 60",
         "30621 Дубленка Леди Зима Длинная шоколад", "60"),
        ("81223_Шуба_куртка удлиненная графит 46",
         "81223_Шуба_куртка удлиненная графит", "46"),
        ("Блузка Adele белая S", "Блузка Adele белая", "S"),
        ("Джинсы Miller синий 32", "Джинсы Miller синий", "32"),
        ("Тренч бежевый", "Тренч бежевый", ""),
        ("25375 дубленка барашек 66 (ДОБАВИТЬ БАРКОД)",
         "25375 дубленка барашек", "66"),
    ]
    for raw, stem_ok, size_ok in cases:
        stem, size = split_size(raw)
        check(f"«{raw[:36]}» → основа", stem == stem_ok, f"got {stem!r}")
        check(f"«{raw[:36]}» → размер", size == size_ok, f"got {size!r}")

    # одно и то же название с разными размерами даёт ОДНУ основу
    base = split_size("3759-72 Дубленка двухсторон_корич 48")[0]
    other = split_size("3759-72 Дубленка двухсторон_корич 54")[0]
    check("размер не влияет на основу", base == other, f"{base!r} != {other!r}")

    # разные цвета дают РАЗНЫЕ основы — это разные позиции
    kor = split_size("3759-72 Дубленка двухсторон_корич 48")[0]
    gra = split_size("3759-72 Дубленка двухсторон_графит 48")[0]
    check("цвет в основе разделяет позиции", kor != gra, f"оба {kor!r}")


def test_net_outflow() -> None:
    print("\nнетто-отток: ярмарка не должна поднимать заказ")
    check("без движения — только продажи", net_outflow(8, 0, 0) == 8.0)
    check("партия на буфер — это отток", net_outflow(8, 20, 0) == 28.0)
    check("возврат с ярмарки отменяет вывоз", net_outflow(8, 20, 5) == 23.0)
    check("увезли и всё вернули — только продажи", net_outflow(8, 20, 20) == 8.0)
    check("возвраты покупателей вычитаются", net_outflow(8, 0, 0, 3) == 5.0)
    check("больше возвратов, чем продаж — ноль", net_outflow(5, 0, 0, 12) == 0.0)
    check("не уходит в минус", net_outflow(0, 10, 30) == 0.0)


def main() -> int:
    test_sizes()
    test_sizes_module()
    test_substitution()
    test_grouping()
    test_build()
    test_tiers()
    test_net_outflow()

    print(f"\n{'=' * 50}")
    print(f"пройдено {PASSES}, провалено {len(FAILS)}")
    if FAILS:
        for f in FAILS:
            print(f"  ✗ {f}")
        return 1
    print("всё чисто")
    return 0


if __name__ == "__main__":
    sys.exit(main())
