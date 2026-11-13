"""Расчёт: сколько заказать, чтобы хватило на следующую неделю.

  продажи в день  = продано за окно / дней в окне
  спрос за неделю = продажи в день × 7 × поправка на сезон
  страховой запас = z × разброс продаж × √7
  заказать        = спрос + страховой − доступно

Доступно — остаток основного склада минус резерв.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class Item:
    sku_id: str
    name: str
    article: str
    color: str = ""
    size: str = ""

    stock: float = 0.0
    reserve: float = 0.0
    available: float = 0.0

    sold: float = 0.0
    daily: float = 0.0
    sigma: float = 0.0
    season: float = 1.0
    profile: str = "default"

    demand: float = 0.0
    safety: float = 0.0
    need: float = 0.0
    cover_days: float | None = None

    barcodes: list[str] = field(default_factory=list)
    wb_url: str = ""


# --------------------------------------------------------------------------
# Сезонность
# --------------------------------------------------------------------------

def month_factor(seasons: list[float], d: date) -> float:
    if not seasons:
        return 1.0
    return float(seasons[d.month - 1])


def season_ratio(seasons: list[float], target: date,
                 history_days: int, today: date) -> float:
    """Во сколько раз спрос в целевом окне отличается от периода измерения.

    Измеренный темп — средний за прошедшие history_days, он уже впитал
    сезонность тех дней. Чтобы применить сезон к будущему, нормируем:

        чистая скорость = факт / средний_коэф_прошлого
        прогноз         = чистая скорость × коэф_целевого_окна
    """
    if not seasons:
        return 1.0

    past = [month_factor(seasons, today - timedelta(days=i))
            for i in range(1, history_days + 1)]
    base = sum(past) / len(past) if past else 1.0
    if base <= 0.01:
        return 1.0

    return round(month_factor(seasons, target) / base, 4)


# --------------------------------------------------------------------------
# Определение сезонного профиля
# --------------------------------------------------------------------------

def classify(text: str, season_map: list) -> str:
    """Профиль по названию товара, категории и папке.

    `text` — склеенная строка из категории, подкатегории, папки и названия.
    `season_map` — список пар [ключ, профиль] от специфичного к общему:
    первое совпадение выигрывает, поэтому «шуба» проверяется раньше
    «куртки», и «куртка зимняя» уходит в зиму, а не в демисезон.
    """
    low = (text or "").lower()
    for pair in season_map or []:
        if not pair or len(pair) < 2:
            continue
        key, profile = str(pair[0]).lower().strip(), str(pair[1])
        if key and key in low:
            return profile
    return "default"


# --------------------------------------------------------------------------
# Основной расчёт
# --------------------------------------------------------------------------

def build(items: dict[str, dict], stock: dict[str, dict], sales: dict[str, float],
          cfg: dict, today: date) -> list[Item]:
    horizon = int(cfg.get("horizon_days", 7))
    window = int(cfg.get("velocity_window_days", 28))
    z = float(cfg.get("service_z", 1.65))
    season_map = cfg.get("season_map") or []
    profiles = cfg.get("profiles") or {}
    target = today + timedelta(days=1)      # окно начинается завтра

    out: list[Item] = []
    for aid, info in items.items():
        st = stock.get(aid)
        sold = sales.get(aid, 0.0)
        if st is None and sold <= 0:
            continue                        # ни остатка, ни продаж

        stock_qty = st["stock"] if st else 0.0
        reserve = st["reserve"] if st else 0.0
        if st and "available" in st:
            available = max(float(st["available"]), 0.0)
        else:
            available = max(stock_qty - reserve, 0.0)

        text = " ".join(filter(None, [
            info.get("category", ""), info.get("subcategory", ""),
            info.get("folder", ""), info.get("name", ""),
        ]))
        profile = classify(text, season_map)
        pcfg = profiles.get(profile) or {}
        seasons = pcfg.get("seasonality") or []

        it = Item(
            sku_id=aid, name=info.get("name", ""),
            article=info.get("article", ""),
            color=info.get("color", ""), size=info.get("size", ""),
            stock=stock_qty, reserve=reserve, available=available,
            sold=sold, barcodes=info.get("barcodes") or [],
            profile=profile,
        )

        it.daily = sold / window if window else 0.0
        # разброс по Пуассону: σ = √N на окно
        it.sigma = math.sqrt(max(sold, 0.0)) / window
        it.season = season_ratio(seasons, target, window, today)

        it.demand = round(it.daily * horizon * it.season, 1)
        it.safety = round(z * it.sigma * math.sqrt(horizon), 1)

        raw = it.demand + it.safety - available
        pack = max(1, int(pcfg.get("pack_qty", 1)))
        it.need = float(math.ceil(raw / pack) * pack) if raw > 0 else 0.0

        if it.daily * it.season > 0.005:
            it.cover_days = round(available / (it.daily * it.season), 0)

        out.append(it)

    return out


def to_order(items: list[Item]) -> list[Item]:
    """Только то, что нужно заказать, — по убыванию потребности."""
    return sorted([i for i in items if i.need > 0],
                  key=lambda x: (-x.need, -x.daily))
