"""Расчёт: сколько заказать, чтобы хватило на следующую неделю.

  продажи в день  = продано за окно / дней в окне
  спрос за неделю = продажи в день × 7 × поправка на сезон
  страховой запас = z × разброс продаж × √7
  заказать        = спрос + страховой − остаток к началу окна − ожидание

Окно — не «завтра», а следующая неделя: с понедельника. Остаток к его
началу меньше сегодняшнего: часть товара уйдёт за дни до понедельника.

Доступно — остаток основного склада минус резерв.
Ожидание (товар в пути на склад) уменьшает потребность, но в
«Доступно» не входит: продать его пока нельзя.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class Item:
    sku_id: str
    name: str
    article: str = ""
    folder: str = ""
    category: str = ""
    subcategory: str = ""
    supplier: str = ""
    color: str = ""
    size: str = ""

    stock: float = 0.0
    reserve: float = 0.0
    available: float = 0.0
    stock_at_start: float = 0.0
    in_transit: float = 0.0

    sold: float = 0.0
    daily: float = 0.0
    season: float = 1.0
    profile: str = "default"

    demand: float = 0.0
    safety: float = 0.0
    need: float = 0.0
    cover_days: float | None = None

    barcodes: list[str] = field(default_factory=list)
    wb_url: str = ""
    wb_how: str = ""


# --------------------------------------------------------------- календарь окна

def window_start(today: date) -> date:
    """Первый день недели, на которую считаем запас.

    Дайджест приходит в середине недели, а закупка нужна на СЛЕДУЮЩУЮ:
    если сегодня суббота 14-го, окно начинается в понедельник 16-го.
    Если запустить в понедельник — окно начинается в тот же день.
    """
    shift = (7 - today.weekday()) % 7      # 0 для понедельника
    return today + timedelta(days=shift)


# ---------------------------------------------------------------- сезонность

def month_factor(seasons: list[float], d: date) -> float:
    if not seasons:
        return 1.0
    return float(seasons[d.month - 1])


def season_ratio(seasons: list[float], target: date,
                 history_days: int, today: date) -> float:
    """Во сколько раз спрос в целевом окне отличается от периода измерения.

    Измеренный темп — средний за прошедшие history_days, он уже впитал
    сезонность тех дней. Нормируем на этот средний, иначе коэффициент
    всегда выходил бы 1.0: темп и цель попадают в один месяц.
    """
    if not seasons:
        return 1.0
    past = [month_factor(seasons, today - timedelta(days=i))
            for i in range(1, history_days + 1)]
    base = sum(past) / len(past) if past else 1.0
    if base <= 0.01:
        return 1.0
    return round(month_factor(seasons, target) / base, 4)


# --------------------------------------------------------- профиль категории

def classify(text: str, season_map: list) -> str:
    """Профиль по категории, подкатегории, папке и названию.

    season_map — пары [ключ, профиль] от специфичного к общему:
    первое совпадение выигрывает, поэтому «шуба» проверяется раньше
    «куртки», и «куртка зимняя» уходит в зиму, а не в демисезон.
    """
    low = (text or "").lower()
    for pair in season_map or []:
        if not pair or len(pair) < 2:
            continue
        key = str(pair[0]).lower().strip()
        if key and key in low:
            return str(pair[1])
    return "default"


# ------------------------------------------------------------ основной расчёт

def build(items: dict[str, dict], stock: dict[str, dict],
          sales: dict[str, float], supplier_names: dict[str, str],
          cfg: dict, today: date) -> list[Item]:
    horizon = int(cfg.get("horizon_days", 7))
    window = int(cfg.get("velocity_window_days", 28))
    z = float(cfg.get("service_z", 1.65))
    season_map = cfg.get("season_map") or []
    profiles = cfg.get("profiles") or {}

    start = window_start(today)
    days_until_start = (start - today).days     # дни до окна
    target = start

    out: list[Item] = []
    for aid, info in items.items():
        st = stock.get(aid)
        sold = sales.get(aid, 0.0)
        if st is None and sold <= 0:
            continue

        stock_qty = float(st["stock"]) if st else 0.0
        reserve = float(st["reserve"]) if st else 0.0
        in_transit = float(st["in_transit"]) if st else 0.0
        available = max(stock_qty - reserve, 0.0)

        text = " ".join(filter(None, [
            info.get("category", ""), info.get("subcategory", ""),
            info.get("folder", ""), info.get("name", ""),
        ]))
        profile = classify(text, season_map)
        pcfg = profiles.get(profile) or {}
        seasons = pcfg.get("seasonality") or []

        sup_id = info.get("supplier_id") or ""
        supplier = supplier_names.get(sup_id, "") if sup_id else ""
        if sup_id and not supplier:
            supplier = "—"

        it = Item(
            sku_id=aid,
            name=info.get("name", ""),
            article=info.get("article", ""),
            folder=info.get("folder", ""),
            category=info.get("category", ""),
            subcategory=info.get("subcategory", ""),
            supplier=supplier,
            color=info.get("color", ""),
            size=info.get("size", ""),
            stock=stock_qty,
            reserve=reserve,
            available=available,
            in_transit=in_transit,
            sold=sold,
            barcodes=info.get("barcodes") or [],
            profile=profile,
        )

        it.daily = sold / window if window else 0.0
        it.season = season_ratio(seasons, target, window, today)

        pace = it.daily * it.season

        # остаток к началу окна: часть товара уйдёт до понедельника
        it.stock_at_start = round(
            max(available - pace * days_until_start, 0.0), 1)

        it.demand = round(pace * horizon, 1)
        it.safety = round(z * math.sqrt(max(sold, 0.0)) / window
                          * math.sqrt(horizon), 1)

        raw = it.demand + it.safety - it.stock_at_start - in_transit
        pack = max(1, int(pcfg.get("pack_qty", 1)))
        it.need = float(math.ceil(raw / pack) * pack) if raw > 0 else 0.0

        # «хватит на N дней» считаем от сегодня — это и есть срочность
        if pace > 0.005:
            it.cover_days = round(available / pace, 0)

        out.append(it)

    return out


def to_order(items: list[Item]) -> list[Item]:
    """Только к заказу, по срочности: сначала то, что кончится раньше."""
    need = [i for i in items if i.need > 0]
    return sorted(need, key=lambda x: (
        x.cover_days if x.cover_days is not None else 999,
        -x.need,
    ))
