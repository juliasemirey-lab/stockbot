"""Демо-выгрузка: баркоды EAN13, поставщики, поступления с ETA.

Баркоды совпадают с тестовым индексом WB (tools/make_wb_index.py),
чтобы можно было проверить сопоставление без сети.
"""
from __future__ import annotations

import json
import random

random.seed(11)
SIZES = ["XS", "S", "M", "L", "XL"]
SUP = ["ООО Текстиль-Опт", "ИП Смирнова", "Marmaris Textile"]

_counter = 0


def ean13() -> str:
    """Генерирует валидный EAN-13 с контрольной цифрой."""
    global _counter
    _counter += 1
    body = f"200{_counter:09d}"[:12]
    s = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
    return body + str((10 - s % 10) % 10)


_NO_BC = object()   # отличает «баркод не задан» от «баркода нет»


def mk(sku_id, article, name, color, size, cat, stock, supplier, reserve=0,
       units=0, days_in_stock=28, arrivals=None, barcode=_NO_BC):
    bc = ean13() if barcode is _NO_BC else barcode
    return dict(
        sku_id=sku_id, article=article, name=name, color=color, size=size,
        category=cat, supplier=supplier, supplier_id="",
        stock=stock, reserve=reserve, units_net=units, days_in_stock=days_in_stock,
        in_transit=sum(a["qty"] for a in (arrivals or [])),
        arrivals=arrivals or [],
        barcodes=[bc] if bc else [],
        barcode_matched="", links={}, link_kind={},
    )


rows = []

# Зимняя верхняя одежда — сезон в разгаре, LT 60
for sz in SIZES:
    rows.append(mk(f"winter-oslo-{sz}", f"WO-OSLO-{sz}", "Пуховик Oslo", "чёрный", sz,
                   "winter_outer", stock=random.choice([2, 4, 6, 12, 18]),
                   supplier="Marmaris Textile", reserve=2, units=random.randint(14, 26)))

# Блузки — три модели, разное состояние
for name, col, base, sup, code in [("Блузка Adele", "белый", 9, "ООО Текстиль-Опт", "ADELE"),
                                   ("Блузка Vera", "синий", 4, "ООО Текстиль-Опт", "VERA"),
                                   ("Блузка Nina", "принт", 0, "ИП Смирнова", "NINA")]:
    for sz in SIZES:
        rows.append(mk(
            f"bl-{code.lower()}-{col}-{sz}", f"BL-{code}-{sz}", name, col, sz,
            "blouses", stock=random.choice([0, 3, 8, 15, 22, 40]), supplier=sup,
            units=max(0, base + random.randint(-3, 3)),
            arrivals=([{"eta_days": 12, "qty": 12}]
                      if sz == "M" and name == "Блузка Adele" else [])))

# Трикотаж
for name, col, sup, code in [("Джемпер Sky", "серый", "ООО Текстиль-Опт", "SKY"),
                             ("Кардиган Moss", "бежевый", "ИП Смирнова", "MOSS")]:
    for sz in SIZES:
        rows.append(mk(f"kn-{code.lower()}-{col}-{sz}", f"KN-{code}-{sz}",
                       name, col, sz, "knitwear", stock=random.choice([4, 9, 16, 30]),
                       supplier=sup, units=random.randint(3, 14)))

# Купальники слитные — сезон закрыт, остатков много
for name, col in [("Купальник Sol", "коралл"), ("Купальник Wave", "чёрный")]:
    for sz in SIZES:
        rows.append(mk(f"sw1-{name[-3:]}-{col}-{sz}", f"SW1-{name[-3:].upper()}-{sz}",
                       name, col, sz, "swim_one_piece", stock=random.choice([30, 45, 60]),
                       supplier="Marmaris Textile", reserve=1, units=random.randint(0, 2)))

# Раздельные — намеренный разбаланс верха и низа
for part in ("верх", "низ"):
    for sz in SIZES:
        is_top = part == "верх"
        rows.append(mk(f"sw2-reno-{part}-{sz}", f"SW2-RENO-{part[:1].upper()}-{sz}",
                       f"Купальник Reno {part}", "изумруд", sz, "swim_separate",
                       stock=random.choice([18, 26]) if is_top else random.choice([2, 5]),
                       supplier="ИП Смирнова",
                       units=random.randint(6, 12) if is_top else random.randint(1, 3),
                       arrivals=([{"eta_days": 20, "qty": 9}]
                                 if not is_top and sz in ("M", "L") else [])))

# Купальные костюмы
for sz in SIZES:
    rows.append(mk(f"swset-bali-{sz}", f"SWS-BALI-{sz}", "Костюм Bali", "полоска", sz,
                   "swim_set", stock=random.choice([0, 6, 14]), supplier="Marmaris Textile",
                   units=random.randint(0, 3), reserve=random.choice([0, 2])))

# Новинка без истории продаж
rows.append(mk("new-blossom-m", "NEW-BLOSSOM-M", "Блузка Blossom (новинка)", "роза", "M",
               "blouses", stock=10, supplier="ООО Текстиль-Опт", units=0, days_in_stock=1))

# Позиция без баркода — проверка диагностики
rows.append(mk("bl-nobc-s", "BL-NOBARCODE-S", "Блузка без баркода", "серый", "S",
               "blouses", stock=5, supplier="ООО Текстиль-Опт", units=6, barcode=None))

# Часть баркодов «не заведена» на WB: исключаем 3 позиции из индекса
print(json.dumps(rows, ensure_ascii=False, indent=1))
