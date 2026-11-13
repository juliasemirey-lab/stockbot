"""Расчётное ядро: планирование на следующую неделю.

Модель отличается от «сколько продадим за 7 дней от сегодня»:

  * окно покрытия — СЛЕДУЮЩАЯ неделя, понедельник–воскресенье;
  * между решением и товаром на складе есть буфер handling_days (3 дня:
    проверка наличия у поставщика, формирование закупки, транспортировка);
  * потребность считается от остатка К НАЧАЛУ ОКНА, а не от сегодняшнего —
    иначе товар, который продастся до понедельника, посчитается дважды;
  * упущенные продажи считаются только внутри окна: то, что теряется
    на этой неделе, уже не исправить закупкой.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta


# --------------------------------------------------------------------------
# Календарь окна
# --------------------------------------------------------------------------


def next_monday(today: date) -> date:
    """Ближайший понедельник строго после today.

    Дайджест приходит в среду, окно — со следующего понедельника. Если
    запустить вручную в понедельник, окно всё равно начнётся через неделю:
    иначе расчёт покрывал бы уже начавшуюся неделю.
    """
    shift = (7 - today.weekday()) % 7
    return today + timedelta(days=shift or 7)


def window_dates(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


# --------------------------------------------------------------------------
# Сезонность
# --------------------------------------------------------------------------


def normalize(vec: list[float]) -> list[float]:
    m = sum(vec) / len(vec)
    if m <= 0:
        return [1.0] * len(vec)
    return [v / m for v in vec]


def month_factor(seasonality: list[float], day: date) -> float:
    s = normalize(seasonality)
    return 0.15 * s[(day.month - 2) % 12] + 0.70 * s[day.month - 1] + 0.15 * s[day.month % 12]


def season_ratio(seasonality: list[float], target: date, history_mid: date) -> float:
    """Отношение сезона целевой даты к сезону месяца, за который измерен темп."""
    base = month_factor(seasonality, history_mid)
    return 1.0 if base <= 0.01 else month_factor(seasonality, target) / base


def season_ratio_days(seasonality: list[float], window: list[date],
                      history_mid: date) -> list[float]:
    """Поправка на сезон по каждому дню окна.

    Берётся ОТНОШЕНИЕ к месяцу, за который измерен темп: базовый темп уже
    сезонный, и умножение на абсолютный коэффициент занизило бы его дважды.
    """
    base = month_factor(seasonality, history_mid)
    if base <= 0.01:
        return [1.0] * len(window)
    return [month_factor(seasonality, d) / base for d in window]


def dow_weights(profile: list[float], window: list[date]) -> list[float]:
    p = normalize(profile)
    return [p[d.weekday()] for d in window]


# --------------------------------------------------------------------------
# Симуляция остатка
# --------------------------------------------------------------------------


@dataclass
class Arrival:
    eta_days: int
    qty: float


def project_series(free: float, daily: float, arrivals: list[Arrival],
                   days: int) -> tuple[list[float], list[float]]:
    """Остаток и упущенные продажи по дням.

    Упущенные продажи — спрос, который нечем закрыть. Для одежды это
    потерянный спрос, а не отложенный, поэтому остаток не уходит в минус.
    """
    stock = free
    series: list[float] = []
    lost: list[float] = []
    for d in range(days):
        stock -= daily
        for a in arrivals:
            if a.eta_days == d:
                stock += a.qty
        short = 0.0
        if stock < 0:
            short, stock = -stock, 0.0
        series.append(stock)
        lost.append(short)
    return series, lost


# --------------------------------------------------------------------------
# Скорость спроса
# --------------------------------------------------------------------------


@dataclass
class Sales:
    units_net: float = 0.0
    days_in_stock: float = 0.0
    window_days: int = 28
    fixed_daily: float | None = None

    @property
    def base_daily(self) -> float:
        if self.fixed_daily is not None:
            return self.fixed_daily
        if self.days_in_stock <= 0:
            return 0.0
        return self.units_net / self.days_in_stock

    @property
    def sigma(self) -> float:
        return 1.3 * math.sqrt(max(self.base_daily, 0.0))


# --------------------------------------------------------------------------
# Строка расчёта
# --------------------------------------------------------------------------


@dataclass
class Line:
    sku_id: str
    name: str
    category: str
    color: str = ""
    size: str = ""
    article: str = ""

    stock: float = 0.0
    reserve: float = 0.0
    in_transit: float = 0.0
    arrivals: list[Arrival] = field(default_factory=list)

    base_daily: float = 0.0
    forecast_daily: float = 0.0
    demand_window: float = 0.0
    lost_window: float = 0.0
    safety: float = 0.0
    need: float = 0.0

    stock_at_window_start: float = 0.0
    cover_days: float | None = None
    days_until_start: int = 0
    arrival_days: int = 3
    arrival_in_time: bool = True

    window_start: date | None = None
    window_end: date | None = None

    supplier: str = ""
    supplier_id: str = ""
    barcodes: list[str] = field(default_factory=list)
    barcode_matched: str = ""
    links: dict = field(default_factory=dict)
    link_kind: dict = field(default_factory=dict)

    status: str = "ok"
    note: str = ""

    @property
    def available(self) -> float:
        return max(0.0, self.stock - self.reserve)

    @property
    def free_stock(self) -> float:
        return self.available

    @property
    def barcode(self) -> str:
        return self.barcode_matched or (self.barcodes[0] if self.barcodes else "")

    @property
    def week_shortage(self) -> bool:
        """Не хватит на следующую неделю — будут упущенные продажи."""
        return self.stock_at_window_start < self.demand_window


def classify(line: Line, seasonality: list[float],
             closed_season_threshold: float = 0.3) -> str:
    if month_factor(seasonality, line.window_start) < closed_season_threshold:
        return "closed_season"
    if line.week_shortage and line.demand_window >= 0.5:
        return "urgent"
    if line.need > 0:
        return "plan"
    if line.base_daily < 0.02 and line.available > 0:
        return "dead"
    return "ok"


def build_lines(rows: list[dict], cfg: dict, today: date) -> list[Line]:
    horizon = int(cfg.get("horizon_days", 7))
    window_days = int(cfg.get("velocity_window_days", 28))
    z = float(cfg.get("service_z", 1.65))
    dow = cfg.get("dow_profile") or [1.0] * 7
    seas_all = cfg.get("seasonality") or {}
    cats = cfg.get("categories") or {}
    sup_cfg = cfg.get("suppliers") or {}
    handling = int((cfg.get("replenishment") or {}).get("handling_days", 3))

    start = next_monday(today)
    win = window_dates(start, horizon)
    days_until_start = (start - today).days
    hist_mid = today - timedelta(days=window_days // 2)
    dow_vec = dow_weights(dow, win)
    total_days = max(days_until_start, 0) + horizon

    out: list[Line] = []
    for r in rows:
        cat = r.get("category") or "blouses"
        ccfg = cats.get(cat, {})
        supplier = r.get("supplier") or ""
        scfg = sup_cfg.get(supplier, {}) if supplier else {}
        arrival_days = int(scfg.get("lead_time_days", handling))

        line = Line(
            sku_id=r["sku_id"], name=r.get("name", ""), category=cat,
            color=r.get("color", ""), size=r.get("size", ""),
            article=r.get("article", ""),
            stock=float(r.get("stock", 0)),
            reserve=float(r.get("reserve", 0)),
            in_transit=float(r.get("in_transit", 0)),
            arrivals=[Arrival(int(a.get("eta_days", 0)), float(a.get("qty", 0)))
                      for a in (r.get("arrivals") or [])],
            supplier=supplier,
            supplier_id=r.get("supplier_id", ""),
            barcodes=r.get("barcodes") or [],
            barcode_matched=r.get("barcode_matched", ""),
            links=r.get("links") or {},
            link_kind=r.get("link_kind") or {},
            window_start=start, window_end=win[-1],
            days_until_start=days_until_start,
            arrival_days=arrival_days,
            arrival_in_time=arrival_days <= days_until_start,
        )

        s = Sales(
            units_net=float(r.get("units_net", 0)),
            days_in_stock=float(r.get("days_in_stock", window_days)),
            window_days=window_days,
            fixed_daily=r.get("fixed_daily"),
        )
        line.base_daily = s.base_daily

        seas = seas_all.get(cat) or [1.0] * 12
        ratios = season_ratio_days(seas, win, hist_mid)
        daily = [line.base_daily * rt * dw for rt, dw in zip(ratios, dow_vec)]
        line.forecast_daily = sum(daily) / len(daily)
        line.demand_window = round(sum(daily), 1)

        # остаток к началу окна и потери внутри окна
        series, lost = project_series(line.available, line.forecast_daily,
                                      line.arrivals, total_days)
        if days_until_start >= 1 and series:
            line.stock_at_window_start = round(series[days_until_start - 1], 1)
        else:
            line.stock_at_window_start = round(line.available, 1)
        line.lost_window = round(sum(lost[days_until_start:days_until_start + horizon]), 1)

        safety = z * s.sigma * math.sqrt(horizon)
        line.safety = round(safety, 1)

        raw = line.demand_window + safety - line.stock_at_window_start
        if raw <= 0:
            line.need = 0.0
        else:
            pack = max(1, int(scfg.get("pack_qty", ccfg.get("pack_qty", 1))))
            line.need = float(math.ceil(raw / pack) * pack)

        line.cover_days = (round(line.stock_at_window_start / line.forecast_daily, 0)
                           if line.forecast_daily > 0.01 else None)
        line.status = classify(line, seas)

        if line.status == "closed_season":
            line.need = 0.0
            line.note = "сезон закрыт — распродажа, не закупка"
        elif line.status == "urgent":
            line.note = (f"на неделю не хватает {line.demand_window - line.stock_at_window_start:.0f} шт, "
                         f"упущенные продажи ~{line.lost_window:.0f}")
        elif line.status == "plan":
            line.note = f"хватит на неделю, но ниже запаса с учётом страховки"
        elif line.status == "dead":
            line.note = "нет продаж и есть остаток"

        if line.need > 0 and not line.arrival_in_time:
            line.note += (f" · поставка {line.arrival_days} дн позже начала окна "
                          f"({line.days_until_start} дн запаса времени)")
        out.append(line)
    return out


def _pair_key(name: str) -> tuple[str, str]:
    low = name.lower()
    if "верх" in low or "топ" in low:
        part = "top"
        for w in ("верх", "топ"):
            low = low.replace(w, "")
    else:
        part = "bottom"
        for w in ("низ", "плавки", "трусы"):
            low = low.replace(w, "")
    return " ".join(low.replace("(", " ").replace(")", " ").split()), part


def pair_separates(lines: list[Line], skip_closed_season: bool = True) -> list[str]:
    groups: dict[tuple, dict[str, list[Line]]] = {}
    for ln in lines:
        if ln.category != "swim_separate":
            continue
        if skip_closed_season and ln.status == "closed_season":
            continue
        model, part = _pair_key(ln.name)
        groups.setdefault((model, ln.color), {"top": [], "bottom": []})[part].append(ln)

    warns = []
    for (model, color), g in groups.items():
        tops, bots = g["top"], g["bottom"]
        if not tops or not bots:
            continue
        t, b = sum(x.need for x in tops), sum(x.need for x in bots)
        if t != b:
            warns.append(f"{model} / {color or '—'}: верх {t:.0f} шт, низ {b:.0f} шт — "
                         f"комплект ограничен меньшей стороной")
    return warns


def group_by_supplier(lines: list[Line]) -> dict[str, list[Line]]:
    out: dict[str, list[Line]] = {}
    for ln in lines:
        if ln.need <= 0:
            continue
        key = ln.supplier or "Поставщик не определён"
        out.setdefault(key, []).append(ln)
    return out


def seasonal_note(cfg: dict, lines: list) -> str:
    """Короткая фраза о сезонном влиянии на окно.

    Берём среднее отношение сезона к годовой базе по непустым категориям
    (где есть продажи и позиции в закупке). Фраза — одна, для шапки.
    """
    if not lines:
        return ""
    seas_all = cfg.get("seasonality") or {}
    if not seas_all:
        return ""
    ws = lines[0].window_start
    hist_mid_shift = int(cfg.get("velocity_window_days", 28)) // 2
    from datetime import timedelta
    hist_mid = ws - timedelta(days=hist_mid_shift)

    cats_active: dict[str, int] = {}
    for ln in lines:
        if ln.base_daily > 0 or ln.need > 0:
            cats_active[ln.category] = cats_active.get(ln.category, 0) + 1
    if not cats_active:
        return ""

    ratios = []
    rising, falling = [], []
    cat_titles = {k: (v or {}).get("title", k)
                  for k, v in (cfg.get("categories") or {}).items()}
    for cat, weight in cats_active.items():
        seas = seas_all.get(cat)
        if not seas:
            continue
        r = season_ratio([float(x) for x in seas], ws, hist_mid)
        ratios.append(r)
        title = cat_titles.get(cat, cat)
        if r >= 1.15:
            rising.append((r, title))
        elif r <= 0.85:
            falling.append((r, title))

    if not ratios:
        return ""
    avg = sum(ratios) / len(ratios)
    if 0.92 <= avg <= 1.08 and not rising and not falling:
        return "с учётом сезонности · ассортимент в межсезонье, спрос ровный"

    rising.sort(reverse=True)
    falling.sort()
    parts = []
    if rising:
        top = ", ".join(t for _, t in rising[:2])
        parts.append(f"растёт спрос: {top}")
    if falling:
        bot_ = ", ".join(t for _, t in falling[:2])
        parts.append(f"уходит сезон: {bot_}")
    if not parts:
        # одинаковое смещение по всем категориям
        direction = "выше" if avg > 1 else "ниже"
        parts.append(f"спрос в целом {direction} среднегодового")
    return "с учётом сезонности · " + "; ".join(parts)
