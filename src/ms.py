"""Клиент МойСклад: ровно три запроса для дайджеста.

  stores()          — склады (id -> имя)
  assortment()      — товары: категория, размер, баркод
  stock(store_id)   — остаток и резерв одного склада
  sales(days)       — продажи за период по всем складам

Продажи берутся из готового отчёта, а не обходом документов:
1973 документа отгрузки — это 1973 запроса, отчёт — один.
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
log = logging.getLogger("stockbot.ms")

BARCODE_FIELDS = ("ean13", "gtin", "ean8", "code128")


class MoySkladError(RuntimeError):
    pass


class MS:
    def __init__(self, token: str | None = None, timeout: float = 60.0):
        token = token or os.environ.get("MOYSKLAD_TOKEN")
        if not token:
            raise MoySkladError("Нет MOYSKLAD_TOKEN")
        self.c = httpx.Client(
            base_url=BASE,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Encoding": "gzip",
                "User-Agent": "stockbot/1.0",
            },
        )

    def rows(self, path: str, params: dict | None = None) -> Iterator[dict]:
        params = dict(params or {})
        offset = 0
        while True:
            q = {**params, "limit": PAGE, "offset": offset}
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
                    time.sleep(max(int(r.headers.get("X-RateLimit-Retry-After", 2)), 1))
                    continue
                if r.status_code == 401:
                    raise MoySkladError("401: токен невалиден или истёк")
                if r.status_code == 403:
                    raise MoySkladError(f"403 на {path}: нет прав у токена")
                if r.status_code == 404:
                    raise MoySkladError(f"404: {path} недоступен")
                if r.status_code == 412:
                    raise MoySkladError(
                        f"412 на {path}: не приняты параметры — {r.text[:200]}")
                if r.status_code >= 500:
                    if attempt == 4:
                        raise MoySkladError(f"{path}: HTTP {r.status_code}")
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                break

            data = r.json()
            chunk = data.get("rows") or []
            yield from chunk
            offset += PAGE
            total = (data.get("meta") or {}).get("size")
            if not chunk or len(chunk) < PAGE:
                break
            if total is not None and offset >= total:
                break

    # --- склады -----------------------------------------------------------
    def stores(self) -> dict[str, str]:
        """id -> имя."""
        return {s["id"]: s.get("name", "") for s in self.rows("/entity/store")}

    def resolve_store(self, name: str) -> str:
        stores = self.stores()
        for sid, sname in stores.items():
            if sname.strip().lower() == name.strip().lower():
                return sid
        for sid, sname in stores.items():
            if name.strip().lower() in sname.lower():
                return sid
        raise MoySkladError(
            f"Склад «{name}» не найден. Есть: {', '.join(sorted(stores.values()))}")

    # --- товары -----------------------------------------------------------
    def assortment(self, exclude: list[str]) -> dict[str, dict]:
        """id -> описание товара.

        Помимо папки собираем «Категория» и «Подкатегория»: в МойСклад это
        может быть как отдельное доп. поле, так и первые уровни дерева
        групп. Берём оба варианта — какой есть.
        """
        out: dict[str, dict] = {}
        skip = [e.lower() for e in (exclude or [])]

        def keep(folder: str) -> bool:
            return not any(s in (folder or "").lower() for s in skip)

        # В API товара поле называется folder, у модификации — product.folder.
        # Варианты expand: сначала с папкой (нужны имена), потом без —
        # если сервер не принял параметр.
        for expand in ("folder", ""):
            try:
                params = {"expand": expand} if expand else None
                for p in self.rows("/entity/product", params):
                    info = _describe(p, None)
                    if not keep(info["folder"]):
                        continue
                    out[p["id"]] = info
                break
            except MoySkladError as e:
                log.info("Товары (expand=%r): %s", expand, e)

        for expand in ("product.folder", "product"):
            try:
                for v in self.rows("/entity/variant", {"expand": expand}):
                    info = _describe(v.get("product") or {}, v)
                    if not keep(info["folder"]):
                        continue
                    out[v["id"]] = info
                break
            except MoySkladError as e:
                log.info("Модификации (expand=%r): %s", expand, e)

        log.info("Товаров: %s", len(out))
        return out

    # --- остатки одного склада -------------------------------------------
    def stock(self, store_id: str) -> dict[str, dict]:
        """Остаток, резерв и доступное по одному складу.

        Основной путь — /report/stock/all с фильтром по складу: он сразу
        отдаёт quantity (доступное = остаток − резерв), без разбора
        вложенного stockByStore. Если фильтр не принят — считаем сами
        из /report/stock/bystore.
        """
        href = f"{BASE}/entity/store/{store_id}"

        try:
            out: dict[str, dict] = {}
            for r in self.rows("/report/stock/all", {"filter": f"store={href}"}):
                aid = r.get("assortmentId") or (r.get("assortment") or {}).get("id")
                if not aid:
                    continue
                stock_qty = float(r.get("stock") or 0)
                reserve = float(r.get("reserve") or 0)
                out[aid] = {
                    "stock": stock_qty,
                    "reserve": reserve,
                    "available": float(r.get("quantity") or max(stock_qty - reserve, 0)),
                }
            if out:
                log.info("Остатки (report/stock/all): %s позиций", len(out))
                return out
            log.info("report/stock/all пуст — берём bystore")
        except MoySkladError as e:
            log.info("report/stock/all: %s — берём bystore", e)

        out = {}
        for r in self.rows("/report/stock/bystore"):
            aid = r.get("assortmentId") or (r.get("assortment") or {}).get("id")
            if not aid:
                continue
            for s in r.get("stockByStore") or []:
                sid = ((s.get("meta") or {}).get("href") or "").rstrip("/").rsplit("/", 1)[-1]
                if sid != store_id:
                    continue
                stock_qty = float(s.get("stock") or 0)
                reserve = float(s.get("reserve") or 0)
                out[aid] = {"stock": stock_qty, "reserve": reserve,
                            "available": max(stock_qty - reserve, 0.0)}
                break
        log.info("Остатки (bystore): %s позиций", len(out))
        return out

    # --- продажи ----------------------------------------------------------
    # Отчёты, где лежат продажи по товарам. Порядок — от самого
    # подходящего к запасному. Пустой ответ не считается успехом:
    # переходим к следующему источнику.
    _SALES = (
        ("/report/turnover/all", {"groupBy": "product"}),
        ("/report/sales/byproduct", {}),
        ("/report/profit/byproduct", {"groupBy": "product"}),
    )

    def sales(self, days: int, debug: bool = False) -> dict[str, float]:
        """Продажи в штуках за период по всем складам: id -> количество.

        Пробуем несколько источников. Первый непустой ответ выигрывает.
        Если все отчёты пусты — идём в документы отгрузки: «продаж нет»
        и «отчёт не отдал данные» — разные вещи, и путать их нельзя.
        """
        until = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S")

        errors: list[str] = []
        for path, extra in self._SALES:
            params = {"momentFrom": since, "momentTo": until, **extra}
            try:
                out: dict[str, float] = {}
                sample = None
                for r in self.rows(path, params):
                    if sample is None:
                        sample = r
                    aid = (r.get("assortment") or {}).get("id")
                    if not aid:
                        continue
                    qty = _quantity(r)
                    if qty:
                        out[aid] = out.get(aid, 0.0) + qty
            except MoySkladError as e:
                log.info("Отчёт %s: %s", path, e)
                errors.append(f"{path}: {e}")
                continue

            if debug and sample is not None:
                log.info("Строка %s: %s", path,
                         {k: sample[k] for k in list(sample)[:12]})

            if out:
                log.info("Продажи (%s): %s позиций за %s дн.", path, len(out), days)
                return out

            log.info("Отчёт %s ответил, но продаж нет — пробуем дальше", path)
            errors.append(f"{path}: ответил пусто")

        log.warning("Отчёты не дали продаж, считаем по документам отгрузки")
        docs = self.sales_from_docs(days)
        if docs:
            log.info("Продажи по документам: %s позиций", len(docs))
            return docs

        log.warning("Продаж не найдено нигде. %s", "; ".join(errors))
        return {}

    def sales_from_docs(self, days: int) -> dict[str, float]:
        """Запасной путь: документы отгрузки и розничные продажи.

        Позиции в списке документов приходят пустыми — их надо
        догружать по ссылке positions.meta.href.
        """
        until = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S")
        filt = f"moment>={since}"

        out: dict[str, float] = {}
        docs_total = 0

        for path in ("/entity/demand", "/entity/retaildemand"):
            try:
                docs = list(self.rows(path, {"filter": filt}))
            except MoySkladError as e:
                log.info("Документы %s: %s", path, e)
                continue

            docs_total += len(docs)
            for doc in docs:
                for pos in self._positions(doc):
                    aid = (pos.get("assortment") or {}).get("id")
                    if not aid:
                        continue
                    q = float(pos.get("quantity") or 0) - float(
                        pos.get("returnedQuantity") or 0)
                    out[aid] = out.get(aid, 0.0) + q

        if docs_total:
            log.info("Документов отгрузки: %s", docs_total)
        return out

    def _positions(self, doc: dict) -> list[dict]:
        """Позиции документа: из ответа либо отдельным запросом по ссылке."""
        pos = doc.get("positions")
        if isinstance(pos, dict):
            rows = pos.get("rows")
            if isinstance(rows, list) and rows:
                return rows
            href = (pos.get("meta") or {}).get("href") or ""
            if href:
                path = href.split("/api/remap/1.2", 1)[-1]
                try:
                    return list(self.rows(path, {"expand": "assortment"}))
                except MoySkladError as e:
                    log.info("Позиции %s: %s", path, e)
        return []

    # --- диагностика ------------------------------------------------------
    def probe(self, days: int = 28) -> list[tuple[str, str]]:
        """Проверяет каждый источник продаж. Возвращает [(источник, итог)]."""
        until = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S")
        report: list[tuple[str, str]] = []

        for path, extra in self._SALES:
            params = {"momentFrom": since, "momentTo": until, **extra}
            try:
                rows = list(self.rows(path, params))
            except MoySkladError as e:
                report.append((path, f"ошибка: {e}"))
                continue
            if not rows:
                report.append((path, "ответил пусто"))
                continue
            qty = sum(_quantity(r) for r in rows)
            keys = ", ".join(list(rows[0])[:8])
            report.append((path, f"{len(rows)} строк, штук {qty:.0f} | поля: {keys}"))

        for path in ("/entity/demand", "/entity/retaildemand"):
            try:
                docs = list(self.rows(path, {"filter": f"moment>={since}"}))
            except MoySkladError as e:
                report.append((path, f"ошибка: {e}"))
                continue
            with_pos = 0
            qty = 0.0
            for d in docs[:20]:                    # выборка, чтобы не ждать
                pos = self._positions(d)
                if pos:
                    with_pos += 1
                    qty += sum(float(p.get("quantity") or 0) for p in pos)
            report.append((path, f"{len(docs)} док., из первых 20 с позициями "
                                 f"{with_pos}, штук {qty:.0f}"))

        return report


def _barcodes(entity: dict) -> list[str]:
    out = []
    for b in entity.get("barcodes") or []:
        for f in BARCODE_FIELDS:
            if b.get(f):
                out.append(str(b[f]))
    return out


def _quantity(row: dict) -> float:
    """Количество проданного из строки отчёта.

    Отчёты кладут его в разные места: outcome.quantity, sellQuantity,
    quantity. Проверяем от точного к общему.
    """
    for path in (("outcome", "quantity"), ("sellQuantity",), ("quantity",)):
        cur = row
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, (int, float)):
            return float(cur)
    return 0.0


def _describe(prod: dict, variant: dict | None) -> dict:
    """Собирает описание товара из карточки и, если есть, модификации."""
    folder_obj = prod.get("productFolder") or prod.get("folder") or {}
    folder = folder_obj.get("pathName") or folder_obj.get("name") or ""

    # первые уровни дерева групп — это и есть «Категория» / «Подкатегория»
    parts = [x.strip() for x in folder.split("/") if x.strip()]
    category = parts[0] if parts else ""
    subcategory = parts[1] if len(parts) > 1 else ""

    # одноимённые доп. поля, если заведены атрибутами
    for a in prod.get("attributes") or []:
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
    for a in (variant or {}).get("attributes") or []:
        nm = (a.get("name") or "").strip().lower()
        val = a.get("value")
        if isinstance(val, dict):
            val = val.get("name") or ""
        val = str(val or "").strip()
        if val and nm in ("категория", "подкатегория"):
            if nm == "категория":
                category = val
            else:
                subcategory = val

    chars = {(c.get("name") or "").strip().lower(): (c.get("value") or "").strip()
             for c in ((variant or {}).get("characteristics") or [])}

    entity = variant or prod
    return {
        "name": prod.get("name") or entity.get("name", ""),
        "folder": folder,
        "category": category,
        "subcategory": subcategory,
        "article": entity.get("article") or entity.get("code") or prod.get("code") or "",
        "barcodes": _barcodes(entity),
        "size": chars.get("размер") or chars.get("size") or "",
        "color": chars.get("цвет") or chars.get("color") or "",
    }
