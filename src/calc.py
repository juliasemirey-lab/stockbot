"""Расчёт: сколько заказать на следующую неделю.

Модель намеренно узкая: считаем потребность ОСНОВНОГО склада.
Остатки на маркетплейсах и в буферах не участвуют — за них отвечают
менеджеры, а нам нужно, чтобы не опустел свой склад.

  доступно        = основной склад − резерв + «Ярмарки» − резерв
  расход в день   = нетто-отток с основного склада / окно
  спрос на неделю = расход × 7 × сезон
  страховка       = z × √(расход) / окно × √7
  заказать        = спрос + страховка − доступно − в пути

Расход — это всё, что ПОКИНУЛО основной склад:
  отгрузки с основного, розничные продажи с основного,
  перемещения с основного на буферы и Ярмарку,
  минус возвраты покупателей и перемещения обратно на основной.

Отгрузки с буферов и с Ярмарки в расход НЕ входят: товар ушёл не с
основного склада. Иначе приёмка сразу на буфер считалась бы дважды.

Пути товара и что попадает в расход:
  приёмка → основной → ФБС        отгрузка с основного
  приёмка → основной → буфер → ФБО  перемещение на буфер
  приёмка → основной → сразу ФБО    отгрузка с основного
  приёмка → буфер → ФБО             ничего
  приёмка → Ярмарки → продажа       ничего
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

# ---------------------------------------------------------------- размеры

# Токены размера: числовые (42, 44, 44.5, 44-46), бра (85E, 90G, 100F)
# и буквенные. Бра идёт первым: иначе «85E» разберётся как «85» и хвост «E»
# останется в названии, ломая группировку.
SIZE_TOKEN = (r'(?:\d{2,3}\s*[A-Ja-j]'
              r'|\d{2}(?:[.,]\d)?'
              r'|\d{2}\s*[-/]\s*\d{2}'
              r'|XXXS|XXS|XS|S|M|L|XL|XXL|XXXL|[2-6]XL'
              r'|ONE\s*SIZE|ОС)')

# Размер может стоять в конце, иногда со скобкой: «52 (54RUS)», «M (48-50)»
SIZE_TAIL = re.compile(
    r'[\s\-_/]*(' + SIZE_TOKEN + r')\s*(?:\([^)]*\))?\s*$', re.I)

# Служебные пометки, которые люди пишут прямо в названии.
# Их надо вырезать до группировки, иначе одна модель распадается на группы.
NOISE = [
    re.compile(r'\(?\s*добавить\s+баркод\s*\)?', re.I),
    re.compile(r'\(?\s*ввести\s+баркод\s*\)?', re.I),
    re.compile(r'\(?\s*актуальн\w*\s+баркод\s*\)?', re.I),
    re.compile(r'не\s+став\w*\s+вирт\w*!*', re.I),
    re.compile(r'\(\s*в\s*тч\s+из\s+[\d\s\-]+\s*\)', re.I),
    re.compile(r'^удалить$', re.I),
]

LETTER_ORDER = {s: i for i, s in enumerate(
    ["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL",
     "2XL", "3XL", "4XL", "5XL", "6XL"])}


def clean_name(name: str) -> str:
    """Убирает служебные пометки и лишние пробелы."""
    out = str(name or "")
    for rx in NOISE:
        out = rx.sub(" ", out)
    out = re.sub(r'\s+', ' ', out).strip(" -_/")
    return out


def split_size(name: str) -> tuple[str, str]:
    """Делит название на (основа, размер). Размер может отсутствовать."""
    n = clean_name(name)
    m = SIZE_TAIL.search(n)
    if not m:
        return n, ""
    stem = n[:m.start()].strip(" -_/")
    return (stem or n), m.group(1).strip().upper().replace(" ", "")


def size_rank(size: str) -> tuple[int, float]:
    """Порядок сортировки: буквенные по своей лестнице, числовые по величине."""
    if not size:
        return (3, 0.0)
    s = size.upper()
    if s in LETTER_ORDER:
        return (0, float(LETTER_ORDER[s]))
    if s in ("ONE SIZE", "ONESIZE", "ОС"):
        return (2, 0.0)
    head = re.split(r'[-/]', s)[0]
    try:
        return (1, float(head.replace(",", ".")))
    except ValueError:
        return (3, 0.0)


def is_letter(size: str) -> bool:
    return size.upper() in LETTER_ORDER


# --------------------------------------------------------------- сезонность

def month_factor(seasons: list[float], d: date) -> float:
    if not seasons or len(seasons) < 12:
        return 1.0
    return float(seasons[d.month - 1])


def season_ratio(seasons: list[float], target: date,
                 history_days: int, today: date) -> float:
    """Во сколько раз спрос в целевом окне отличается от периода измерения.

    Измеренный темп — средний за прошедшие history_days, он уже впитал
    сезонность тех дней. Нормируем на этот средний, иначе коэффициент
    всегда выходил бы 1.0.
    """
    if not seasons or len(seasons) < 12:
        return 1.0
    past = [month_factor(seasons, today - timedelta(days=i))
            for i in range(1, history_days + 1)]
    base = sum(past) / len(past) if past else 1.0
    if base <= 0.01:
        return 1.0
    return round(month_factor(seasons, target) / base, 4)


def window_start(today: date) -> date:
    """Первый день недели, на которую считаем запас.

    Дайджест приходит в середине недели, а закупка нужна на СЛЕДУЮЩУЮ:
    если сегодня суббота 14-го, окно начинается в понедельник 16-го.
    Если запустить в понедельник — окно начинается в тот же день.
    """
    shift = (7 - today.weekday()) % 7
    return today + timedelta(days=shift)


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


# ------------------------------------------------------------ расчёт позиции

@dataclass
class Item:
    sku_id: str
    name: str
    article: str = ""
    supplier: str = ""
    supplier_id: str = ""
    folder: str = ""

    stem: str = ""          # название без размера
    stem_display: str = ""  # то же в исходном регистре — для вывода
    size: str = ""

    stock: float = 0.0
    reserve: float = 0.0
    in_transit: float = 0.0
    available: float = 0.0
    yard: float = 0.0        # остаток на складе «Ярмарки»: товар наш, но не на полке

    sold: float = 0.0
    daily: float = 0.0
    season: float = 1.0

    demand: float = 0.0
    safety: float = 0.0
    need: float = 0.0
    hard: float = 0.0       # дефицит БЕЗ страхового запаса: реально кончается
    cover_days: float | None = None

    barcodes: list[str] = field(default_factory=list)
    wb_url: str = ""


@dataclass
class Group:
    """Позиция у одного поставщика — все её размеры."""
    stem: str
    supplier: str
    supplier_id: str
    sizes: list[Item] = field(default_factory=list)
    stem_display: str = ""

    # заполняется после подмены
    after_move: dict[str, float] = field(default_factory=dict)
    moves: list[tuple[str, float, str]] = field(default_factory=list)

    def sorted_items(self) -> list[Item]:
        return sorted(self.sizes, key=lambda s: size_rank(s.size))

    @property
    def total_order(self) -> float:
        """Итог к заказу — ПОСЛЕ подмен.

        Поэтому он может быть меньше суммы чисел в строке размеров: там
        показана потребность до подмены, здесь — то, что реально надо купить.
        Расхождение означает, что часть дефицита закрыта перемаркировкой.
        """
        return sum(self.after_move.values()) if self.after_move else \
            sum(s.need for s in self.sizes)

    @property
    def hard_total(self) -> float:
        """Дефицит без страхового запаса — мера реального опустошения позиции.

        По ней решается, горит ли позиция. Заказ при этом считается по total_order:
        покупаем с подушкой, но срочность определяем по тому, что кончается.
        """
        return sum(s.hard for s in self.sizes)

    @property
    def hard_depleted_share(self) -> float:
        """Доля размеров, у которых кончается покрытие БЕЗ страховой надбавки."""
        if not self.sizes:
            return 0.0
        return sum(1 for s in self.sizes if s.hard > 0) / len(self.sizes)

    @property
    def sold_total(self) -> float:
        return sum(s.sold for s in self.sizes)

    @property
    def yard_total(self) -> float:
        return sum(s.yard for s in self.sizes)

    @property
    def size_count(self) -> int:
        return len(self.sizes)

    @property
    def depleted_share(self) -> float:
        """Доля размеров, которые надо заказать. 1.0 — вымыта вся сетка."""
        if not self.sizes:
            return 0.0
        return sum(1 for s in self.sizes if s.need > 0) / len(self.sizes)

    @property
    def wb_url(self) -> str:
        for s in self.sizes:
            if s.wb_url:
                return s.wb_url
        return ""


def net_outflow(sold: float, moved_out: float, moved_in: float,
                returned: float = 0.0) -> float:
    """Нетто-отток с основного склада за окно — что ушло и не вернулось.

    Все аргументы в штуках за окно наблюдения, не в документах:

      sold       отгрузки и розничные продажи С основного склада
      moved_out  перемещения с основного на буферы ФБО и на склад Ярмарки
      moved_in   перемещения обратно на основной (возврат с Ярмарки, отмена партии)
      returned   возвраты покупателей

    Партия на буфер ФБО уходит безвозвратно — это отток. Ярмарка возвращается,
    её возврат вычитается здесь, а остаток на складе «Ярмарки» входит в покрытие
    спроса в build(). Вместе это не даёт ни двойного счёта, ни ложной закупки
    товара, который просто уехал на выезд.

    Минус (возвратов больше, чем ушло) обрезается в ноль: отрицательная скорость
    ушла бы в спрос и страховой запас.
    """
    return max(sold + moved_out - moved_in - returned, 0.0)


def build(items: dict[str, dict], stock: dict[str, dict],
          sales: dict[str, float], supplier_names: dict[str, str],
          cfg: dict, today: date,
          yard: dict[str, dict] | None = None) -> list[Item]:
    """yard — остатки склада «Ярмарки».

    Ярмарка входит в покрытие спроса, но НЕ в показываемый остаток: товар
    физически в другом месте, кладовщик его не найдёт, а на маркетплейс он
    не выгружается. Поэтому available — только основной склад (именно это
    число видит закупщик и маркетплейс), а need считается от суммы.
    """
    horizon = int(cfg.get("horizon_days", 7))
    window = int(cfg.get("velocity_window_days", 28))
    z = float(cfg.get("service_z", 1.65))
    season_map = cfg.get("season_map") or []
    profiles = cfg.get("profiles") or {}
    target = window_start(today)
    yard = yard or {}

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

        yt = yard.get(aid)
        yard_qty = max(float(yt["stock"]) - float(yt["reserve"]), 0.0) if yt else 0.0

        # Позиция — это полное название без размера. Цвет входит в название и
        # отдельной модификацией не является: в МойСкладе одна карточка = один
        # цвет, размеры — её модификации. Поэтому режем ТОЛЬКО размер и ничего
        # больше не трогаем: любое «умное» вырезание слов вроде «великан» или
        # «шоколад» искажает основу и рвёт размерную сетку.
        stem, size = split_size(info.get("name", ""))
        stem_display = stem

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
            name=clean_name(info.get("name", "")),
            article=info.get("article", ""),
            supplier=supplier,
            supplier_id=sup_id,
            folder=info.get("folder", ""),
            stem=stem,
            stem_display=stem_display,
            size=size,
            stock=stock_qty,
            reserve=reserve,
            in_transit=in_transit,
            available=available,
            yard=yard_qty,
            sold=sold,
            barcodes=info.get("barcodes") or [],
        )

        it.daily = sold / window if window else 0.0
        it.season = season_ratio(seasons, target, window, today)

        it.demand = it.daily * horizon * it.season
        it.safety = (z * math.sqrt(max(sold, 0.0)) / window
                     * math.sqrt(horizon)) if window else 0.0

        raw = it.demand + it.safety - (available + yard_qty) - in_transit
        pack = max(1, int(pcfg.get("pack_qty", 1)))
        it.need = float(math.ceil(raw / pack) * pack) if raw > 0 else 0.0

        # Дефицит без страхового запаса. Отвечает на вопрос «кончится ли товар
        # на этой неделе», а не «хватит ли подушки». Именно он делает позицию
        # горящей: страховая надбавка — планирование, она не создаёт срочность.
        hard_raw = it.demand - (available + yard_qty) - in_transit
        it.hard = float(math.ceil(hard_raw / pack) * pack) if hard_raw > 0 else 0.0

        pace = it.daily * it.season
        if pace > 0.005:
            it.cover_days = available / pace

        out.append(it)

    return out


# ------------------------------------------------------------------ подмена

def _one_step(a: Item, b: Item) -> bool:
    """Соседние ли размеры: ровно один шаг лестницы.

    Числовые — разница 2 (44→46, 46→48). Буквенные — соседние по таблице.
    Разрыв 46→52 (два пропущенных размера) — это уже другой товар,
    а не перемаркировка.
    """
    ka, va = size_rank(a.size)
    kb, vb = size_rank(b.size)
    if ka != kb or ka == 3:
        return False
    return abs(va - vb) <= (1.0 if ka == 0 else 2.0)


def apply_substitution(groups: list[Group]) -> None:
    """Закрывает дефицит размера за счёт соседнего размера.

    Донор — только соседний размер того же типа (числовые с числовыми,
    буквенные с буквенными), отдаёт излишек сверх собственной потребности.
    Донор не может сам быть в дефиците.

    Излишек считается один раз: before — снимок потребности до всех подмен.
    Иначе размер, чья потребность уже закрыта предыдущей подменой, снова
    выглядел бы дефицитным, и один донор отдал бы дважды.
    """
    for g in groups:
        ordered = sorted(g.sizes, key=lambda s: size_rank(s.size))

        # потребность ДО подмен — неизменяемый снимок
        before = {s.size: max(s.need, 0.0) for s in ordered}
        after = dict(before)

        # сколько размер уже отдал: ограничивает суммарную отдачу излишком
        given: dict[str, float] = {}
        moves: list[tuple[str, float, str]] = []

        for idx, s in enumerate(ordered):
            if after[s.size] <= 0:
                continue
            for nb in (idx - 1, idx + 1):
                if not (0 <= nb < len(ordered)):
                    continue
                d = ordered[nb]
                if d.size == s.size or is_letter(d.size) != is_letter(s.size):
                    continue
                if not _one_step(s, d):
                    continue
                # донор не должен сам нуждаться в заказе — сверяемся со
                # снимком, а не с уже уменьшенным after
                if before.get(d.size, 0) > 0:
                    continue

                spare = max(d.available - d.demand - d.safety, 0.0)
                left = spare - given.get(d.size, 0.0)
                if left <= 0:
                    continue

                # перемаркировать можно только целые штуки: 0.4 шт физически
                # не существует, а в дайджесте это давало «перемаркировать 0 шт»
                take = math.floor(min(left, after[s.size]))
                if take < 1:
                    continue

                after[s.size] -= take
                given[d.size] = given.get(d.size, 0.0) + float(take)
                moves.append((d.size, float(take), s.size))
                d.available -= take
                if after[s.size] <= 0:
                    break

        g.after_move = after
        g.moves = moves


def make_groups(need: list[Item]) -> list[Group]:
    """Группирует позиции к заказу.

    Ключ — основа названия + поставщик. Цвет входит в основу: он записан прямо
    в названии и отдельной модификацией не является. Отдельно только размер.
    """
    acc: dict[tuple[str, str], Group] = {}
    for it in need:
        key = (it.stem, it.supplier_id or it.supplier)
        g = acc.get(key)
        if g is None:
            g = acc[key] = Group(stem=it.stem, supplier=it.supplier,
                                 supplier_id=it.supplier_id,
                                 stem_display=it.stem_display)
        g.sizes.append(it)

    groups = list(acc.values())
    for g in groups:
        g.sizes.sort(key=lambda s: size_rank(s.size))

    apply_substitution(groups)
    return groups


def make_groups_full(all_items: list[Item]) -> list[Group]:
    """Группы по ВСЕМУ каталогу — нужны, чтобы знать полный размерный ряд.

    Доля вымытой сетки считается как «сколько размеров надо заказать» делить
    на «сколько размеров вообще есть у модели». Если строить группы только по
    позициям к заказу, знаменатель всегда равен числителю и признак «вымыта
    сетка» теряет смысл.
    """
    return make_groups(all_items)


# ------------------------------------------------------------------- ярусы

def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def split_tiers(groups: list[Group], cfg: dict) -> tuple[list[Group], list[Group]]:
    """Делит группы на «горит» и «остальное».

    Порог «горит» — не абсолютное число штук, а сочетание четырёх признаков,
    которое описывает реально вымывающуюся ходовую позицию:

      * сетка не меньше 3 размеров — одиночная позиция (ONE SIZE, кардиган)
        не бывает «вымытой сеткой», это просто одна позиция;
      * вымыто не меньше hot_share размеров ПО РЕАЛЬНОМУ ДЕФИЦИТУ — если пропал
        один размер из семи, это дыра, а не пожар. Считаем без страхового
        запаса: подушка просела — это плановая докупка, а не пожар;
      * продажи позиции не ниже hot_quantile от когорты — аутсайдеры не горят,
        сколько бы размеров у них ни кончилось;
      * суммарный дефицит по реальному спросу не ниже hot_min_qty — иначе это
        «две штучки», за которыми закупщик не поедет.

    Заказ при этом считается С подушкой (total_order): её всё равно надо
    восполнять, просто это уже плановая докупка, а не срочная.

    Квартиль считается по самой когорте, поэтому порог растёт вместе с
    бизнесом: не нужно переписывать константы при росте продаж.
    """
    with_need = [g for g in groups if g.total_order > 0]
    if not with_need:
        return [], []

    q = float(cfg.get("hot_quantile", 0.75))
    sales_cut = _quantile([g.sold_total for g in with_need], q)

    share_min = float(cfg.get("hot_share", 0.70))
    size_min = int(cfg.get("hot_size_min", 3))
    qty_min = float(cfg.get("hot_min_qty", 6))

    hot, rest = [], []
    for g in with_need:
        # Признаки «горит» считаются по дефициту БЕЗ страхового запаса:
        # позиция горячая, когда товар реально кончается, а не когда проседает
        # подушка. Страховой запас — планирование, он не создаёт срочность.
        is_hot = (g.size_count >= size_min
                  and g.hard_depleted_share >= share_min
                  and g.sold_total >= sales_cut
                  and g.hard_total >= qty_min)
        (hot if is_hot else rest).append(g)

    hot.sort(key=lambda g: -g.total_order)
    rest.sort(key=lambda g: -g.total_order)
    return hot, rest

