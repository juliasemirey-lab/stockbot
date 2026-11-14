"""Клиент МойСклад: товары, остатки, продажи, поставщики.

Источники:
  /entity/product            товары + баркоды + поставщик + папка
  /report/stock/bystore      остаток/резерв/ожидание по складам
  /entity/demand             отгрузки
  /entity/retaildemand       розничные продажи
  /entity/counterparty       имена поставщиков
  /entity/organization       тоже (поставщик может ссылаться на организацию)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

import httpx

BASE = "https://api.moysklad.ru/api/remap/1.2"
PAGE = 1000
EXPAND_PAGE = 100          # МойСклад раскрывает вложенные поля только при limit<=100

BARCODE_FIELDS = ("ean13", "gtin", "ean8", "code128")
PRICE_DIVISOR = 100        # buyPrice приходит в копейках

log = logging.getLogger("stockbot.ms")


class MoySkladError(RuntimeError):
    pass


def period(days: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (now - timedelta(days=days)).strftime(fmt), now.strftime(fmt)


class MS:
    def __init__(self, token: str | None = None, timeout: float = 90.0):
        token = token or os.environ.get("MOYSKLAD_TOKEN")
        if not token:
            raise MoySkladError("Нет MOYSKLAD_TOKEN")
        self.c = httpx.Client(
            base_url=BASE,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Encoding": "gzip",
                "User-Agent": "stockbot/5.0",
            },
        )

    # ------------------------------------------------------------------ база
    def rows(self, path: str, params: dict | None = None,
             page: int = PAGE) -> Iterator[dict]:
        params = dict(params or {})
        offset = 0
        while True:
            q = {**params, "limit": page, "offset": offset}
            r = None
            for attempt in range(5):
                try:
                    r = self.c.get(path, params=q)
                except httpx.TransportError as e:
                    if attempt == 4:
                        raise MoySkladError(f"{path}: {e!r}") from e
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code == 429:
                    time.sleep(max(int(r.headers.get("X-RateLimit-Retry-After", 3)), 1))
                    continue
                if r.status_code == 401:
                    raise MoySkladError("401: токен невалиден или истёк")
                if r.status_code == 403:
                    raise MoySkladError(f"403 на {path}: нет прав у токена")
                if r.status_code == 404:
                    raise MoySkladError(f"404: {path} недоступен")
                if r.status_code >= 500:
                    if attempt == 4:
                        raise MoySkladError(f"{path}: HTTP {r.status_code}")
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code >= 400:
                    raise MoySkladError(
                        f"HTTP {r.status_code} на {path}: {r.text[:200]}")
                break

            data = r.json()
            chunk = data.get("rows") or []
            yield from chunk
            offset += page
            total = (data.get("meta") or {}).get("size")
            if not chunk or len(chunk) < page:
                break
            if total is not None and offset >= total:
                break

    # --------------------------------------------------------------- склады
    def stores(self) -> dict[str, str]:
        return {s["id"]: s.get("name", "") for s in self.rows("/entity/store")}

    def resolve_store(self, name: str) -> str:
        stores = self.stores()
        low = name.strip().lower()
        for sid, sname in stores.items():
            if sname.strip().lower() == low:
                return sid
        raise MoySkladError(
            f"Склад «{name}» не найден. Есть: "
            + ", ".join(sorted(stores.values())))

    # --------------------------------------------------------------- товары
    def assortment(self, exclude: list[str] | None = None) -> dict[str, dict]:
        """id -> {name, folder, category, subcategory, article,
                  barcodes, supplier, buy_price}.

        Папка раскрывается только при limit=100: при большем лимите
        МойСклад молча выбрасывает expand, и все товары уходят
        в категорию «Всесезон».
        """
        out: dict[str, dict] = {}
        skip = [e.lower() for e in (exclude or [])]

        for p in self.rows("/entity/product", {"expand": "folder"},
                           page=EXPAND_PAGE):
            info = describe(p)
            if any(s in info["folder"].lower() for s in skip):
                continue
            out[p["id"]] = info

        log.info("Товаров: %s", len(out))
        return out

    # ------------------------------------------------------------- поставщики
    def supplier_names(self) -> dict[str, str]:
        """id -> имя. Контрагенты И организации, включая архивные.

        Поставщик в карточке товара может ссылаться на любой из этих
        типов. Архивные тоже нужны: иначе часть поставщиков останется
        без имени.
        """
        names: dict[str, str] = {}
        for path in ("/entity/counterparty", "/entity/organization"):
            for filt in (None, "archived=true"):
                params = {"filter": filt} if filt else None
                try:
                    for c in self.rows(path, params):
                        names[c["id"]] = c.get("name", "")
                except MoySkladError as e:
                    log.info("%s: %s", path, e)
        log.info("Поставщиков в справочнике: %s", len(names))
        return names

    # --------------------------------------------------------------- остатки
    def stock(self, store_name: str) -> dict[str, dict]:
        """Остаток, резерв, ожидание по одному складу.

        Источник — /report/stock/bystore. Склад внутри stockByStore
        определяется полем name, а не ссылкой: ссылка может прийти
        с довеском, и сравнение по ней молча не проходит — тогда
        остаток оказывается нулевым у всего склада.

        stock    — физический остаток, резерв в нём НЕ вычтен
        inTransit — ожидание, товар в пути НА склад
        """
        want = store_name.strip()
        out: dict[str, dict] = {}
        seen_stores: set[str] = set()

        for r in self.rows("/report/stock/bystore"):
            doc_id = _id_from_href((r.get("meta") or {}).get("href", ""))
            aid = r.get("assortmentId") or doc_id
            if not aid:
                continue

            for s in r.get("stockByStore") or []:
                name = (s.get("name") or "").strip()
                seen_stores.add(name)
                if name != want:
                    continue

                stock_qty = float(s.get("stock") or 0)
                reserve = float(s.get("reserve") or 0)
                in_transit = float(s.get("inTransit") or 0)

                rec = out.setdefault(aid, {"stock": 0.0, "reserve": 0.0,
                                           "in_transit": 0.0})
                rec["stock"] += stock_qty
                rec["reserve"] += reserve
                rec["in_transit"] += in_transit

        if not out:
            raise MoySkladError(
                f"Склад «{want}» не встретился ни у одной позиции. "
                f"В отчёте есть склады: {', '.join(sorted(seen_stores)) or '—'}")

        log.info("Остатки по «%s»: %s позиций", want, len(out))
        return out

    # --------------------------------------------------------------- продажи
    def sales(self, days: int = 28) -> dict[str, float]:
        """Продажи в штуках за период по всем складам: id -> количество.

        Берём ВСЕ документы отгрузки и розничные продажи, склад не
        разбираем: товар ушёл с любого склада — спрос состоялся.

        Возвраты не вычитаются. По схеме комиссионера отгрузка на
        маркетплейс — это уже продажа; возврат приходит отдельным
        документом и оприходуется обратно на основной склад, то есть
        он уже сидит в остатке. Вычесть его ещё и из продаж — значит
        занизить спрос дважды.
        """
        since, _ = period(days)
        out: dict[str, float] = {}
        docs_total = 0
        inline = 0

        for path in ("/entity/demand", "/entity/retaildemand"):
            try:
                for doc in self.rows(path,
                                     {"filter": f"moment>={since}",
                                      "expand": "positions.assortment"},
                                     page=EXPAND_PAGE):
                    docs_total += 1
                    pos = doc.get("positions")
                    rows = pos.get("rows") if isinstance(pos, dict) else None
                    if not isinstance(rows, list) or not rows:
                        continue
                    inline += 1
                    for p in rows:
                        aid = (p.get("assortment") or {}).get("id")
                        if not aid:
                            continue
                        q = float(p.get("quantity") or 0)
                        if q:
                            out[aid] = out.get(aid, 0.0) + q
            except MoySkladError as e:
                log.info("%s: %s", path, e)

        log.info("Документов: %s, с позициями в ответе: %s", docs_total, inline)
        log.info("Продажи: %s позиций, %s шт.", len(out), sum(out.values()))
        return out

    # ---------------------------------------------------------- диагностика
    def probe(self, days: int = 28, store_name: str = "") -> list[tuple[str, str]]:
        """Короткая проверка источников — на случай странных цифр."""
        since, _ = period(days)
        report: list[tuple[str, str]] = []

        try:
            stores = self.stores()
            report.append(("склады", ", ".join(sorted(stores.values()))))
        except MoySkladError as e:
            report.append(("склады", f"ошибка: {e}"))

        for path in ("/entity/demand", "/entity/retaildemand"):
            try:
                docs = list(self.rows(path,
                                      {"filter": f"moment>={since}",
                                       "expand": "positions.assortment"},
                                      page=EXPAND_PAGE))
            except MoySkladError as e:
                report.append((path, f"ошибка: {e}"))
                continue
            with_rows = sum(1 for d in docs
                            if isinstance(d.get("positions"), dict)
                            and d["positions"].get("rows"))
            qty = sum(float(p.get("quantity") or 0)
                      for d in docs
                      if isinstance(d.get("positions"), dict)
                      for p in d["positions"].get("rows") or [])
            report.append((path, f"{len(docs)} док. · с позициями {with_rows} "
                                 f"· штук {qty:.0f}"))

        if store_name:
            try:
                st = self.stock(store_name)
                total = sum(v["stock"] for v in st.values())
                report.append((f"остатки «{store_name}»",
                               f"{len(st)} позиций · штук {total:.0f}"))
            except MoySkladError as e:
                report.append((f"остатки «{store_name}»", f"ошибка: {e}"))

        return report


def _id_from_href(href: str) -> str:
    """Последний сегмент ссылки без query-хвоста."""
    if not href:
        return ""
    return href.split("?")[0].rstrip("/").rsplit("/", 1)[-1]


def _barcodes(entity: dict) -> list[str]:
    """Все баркоды карточки: только цифры, длина 13 или 14."""
    out: list[str] = []
    for b in entity.get("barcodes") or []:
        for field in BARCODE_FIELDS:
            raw = b.get(field)
            if not raw:
                continue
            code = "".join(ch for ch in str(raw) if ch.isdigit())
            if len(code) in (13, 14) and code not in out:
                out.append(code)
    return out


def describe(p: dict) -> dict:
    """Поля карточки товара, нужные расчёту."""
    folder_obj = p.get("folder") or {}
    folder = folder_obj.get("pathName") or folder_obj.get("name") or ""
    parts = [x.strip() for x in folder.split("/") if x.strip()]

    category = parts[0] if parts else ""
    subcategory = parts[1] if len(parts) > 1 else ""

    for a in p.get("attributes") or []:
        nm = (a.get("name") or "").strip().lower()
        val = a.get("value")
        if isinstance(val, dict):
            val = val.get("name") or ""
        val = str(val or "").strip()
        if not val:
            continue
        if nm in ("категория", "category"):
            category = val
        elif nm in ("подкатегория", "subcategory"):
            subcategory = val

    supplier_id = ""
    sup = p.get("supplier")
    if isinstance(sup, dict):
        supplier_id = _id_from_href((sup.get("meta") or {}).get("href", ""))

    price = None
    bp = p.get("buyPrice")
    if isinstance(bp, dict) and bp.get("value") is not None:
        price = float(bp["value"]) / PRICE_DIVISOR

    return {
        "name": p.get("name", ""),
        "folder": folder,
        "category": category,
        "subcategory": subcategory,
        "article": p.get("article") or p.get("code") or "",
        "barcodes": _barcodes(p),
        "supplier_id": supplier_id,
        "buy_price": price,
        "size": "",
        "color": "",
    }
