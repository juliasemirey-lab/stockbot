"""Клиент API МойСклад 1.2: пагинация, лимиты, справочники, остатки, поступления."""
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

BARCODE_KEYS = ("ean13", "gtin", "ean8", "upc", "code128")


class MoySkladError(RuntimeError):
    pass


class MS:
    def __init__(self, token: str | None = None, base: str = BASE, timeout: float = 90.0):
        token = token or os.environ.get("MOYSKLAD_TOKEN")
        if not token:
            raise MoySkladError("Нет токена: задайте MOYSKLAD_TOKEN или используйте --dump.")
        self.c = httpx.Client(
            base_url=base,
            headers={"Authorization": f"Bearer {token}",
                     "Accept-Encoding": "gzip", "User-Agent": "stockbot/0.3"},
            timeout=timeout,
        )

    # --- низкий уровень -------------------------------------------------
    def rows(self, path: str, params: dict | None = None, page: int = PAGE) -> Iterator[dict]:
        params = dict(params or {})
        offset = 0
        while True:
            q = {**params, "limit": page, "offset": offset}
            r = None
            for attempt in range(6):
                try:
                    r = self.c.get(path, params=q)
                except httpx.TransportError as e:
                    if attempt == 5:
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
                    return
                if r.status_code == 412:
                    body = r.text[:300].replace("\n", " ")
                    raise MoySkladError(
                        f"412 на {path}: МойСклад не принял параметры запроса. {body}")
                if 400 <= r.status_code < 500:
                    body = r.text[:300].replace("\n", " ")
                    raise MoySkladError(f"HTTP {r.status_code} на {path}: {body}")
                if r.status_code >= 500:
                    if attempt == 5:
                        raise MoySkladError(f"{path}: HTTP {r.status_code}")
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                break
            data = r.json()
            chunk = data.get("rows") or data.get("result") or []
            yield from chunk
            total = (data.get("meta") or {}).get("size")
            offset += page
            if not chunk or len(chunk) < page:
                break
            if total is not None and offset >= total:
                break

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self.c.get(path, params=params or {})
        r.raise_for_status()
        return r.json()

    # --- справочник номенклатуры ---------------------------------------
    def assortment_meta(self) -> dict[str, dict]:
        out: dict[str, dict] = {}

        for p in self.rows("/entity/product", params={"expand": "folder,attributes"}):
            out[p["id"]] = {
                "name": p.get("name", ""),
                "folder": (p.get("folder") or {}).get("name", ""),
                "color": "", "size": "",
                "article": p.get("article") or p.get("code") or "",
                "attrs": _attrs(p),
                "barcodes": _barcodes(p),
                "product_id": p["id"],
            }

        for v in self.rows("/entity/variant",
                           params={"expand": "product.folder,characteristics,attributes"}):
            prod = v.get("product") or {}
            color = size = ""
            for ch in v.get("characteristics") or []:
                nm = (ch.get("name") or "").lower()
                val = ch.get("value") or ""
                if "цвет" in nm:
                    color = val
                elif "размер" in nm:
                    size = val
            attrs = dict(_attrs(prod))
            attrs.update(_attrs(v))
            out[v["id"]] = {
                "name": prod.get("name") or v.get("name", ""),
                "folder": (prod.get("folder") or {}).get("name", ""),
                "color": color, "size": size,
                "article": v.get("article") or v.get("code") or "",
                "attrs": attrs,
                "barcodes": _barcodes(v) or _barcodes(prod),
                "product_id": prod.get("id") or v["id"],
            }
        return out

    # --- склады и остатки -----------------------------------------------
    def stores(self) -> list[dict]:
        return list(self.rows("/entity/store"))

    def resolve_store(self, name: str) -> dict:
        n = (name or "").strip().lower()
        if not n:
            raise MoySkladError("Не задан склад в config.yaml → warehouse")
        allstores = self.stores()
        exact = [s for s in allstores if (s.get("name") or "").lower() == n]
        if exact:
            return exact[0]
        part = [s for s in allstores if n in (s.get("name") or "").lower()]
        if len(part) == 1:
            return part[0]
        names = ", ".join(s.get("name", "") for s in allstores)
        if len(part) > 1:
            raise MoySkladError(f"Склад «{name}» неоднозначен: "
                                + ", ".join(s.get("name", "") for s in part))
        raise MoySkladError(f"Склад «{name}» не найден. Доступны: {names}")

    def stock(self, store_name: str | None = None) -> dict[str, dict]:
        """Остатки. При заданном складе берём только его.

        /report/stock/all/current не фильтруется по складу — при нескольких
        складах он даёт сумму, и резерв «Основного» смешивается с остальными.
        Поэтому используем /report/stock/bystore и берём нужную точку.
        """
        if not store_name:
            out = {}
            for r in self.rows("/report/stock/all/current"):
                aid = r.get("assortmentId") or r.get("id")
                if aid:
                    out[aid] = {"stock": float(r.get("stock") or 0),
                                "reserve": float(r.get("reserve") or 0),
                                "intransit": float(r.get("intransit") or 0)}
            return out

        store = self.resolve_store(store_name)
        store_id = store["id"]

        out: dict[str, dict] = {}
        for r in self.rows("/report/stock/bystore"):
            aid = r.get("assortmentId")
            if not aid:
                href = (r.get("meta") or {}).get("href", "")
                aid = href.rsplit("/", 1)[-1] if href else ""
            if not aid:
                continue
            stock = reserve = 0.0
            for bs in r.get("stockByStore") or []:
                sid = (bs.get("meta") or {}).get("href", "").rsplit("/", 1)[-1]
                if sid and sid == store_id:
                    stock += float(bs.get("stock") or 0)
                    reserve += float(bs.get("reserve") or 0)
            out[aid] = {"stock": stock, "reserve": reserve, "intransit": 0.0}
        return out

    def reserve_from_orders(self, store_name: str | None = None) -> dict[str, float]:
        """Резерв из неотгруженных заказов покупателей — запасной источник.

        В МойСклад фильтр `shipped=false` на customerorder на части
        конфигураций отдаёт 412: состав фильтруемых полей зависит от
        тарифа и включённых модулей. Поэтому пробуем несколько вариантов
        от простого к сложному, а если ни один не прошёл — возвращаем
        пусто: основной расчёт падать из-за этого не должен, резерв
        возьмётся из отчёта об остатках.

        Отдаёт: assortmentId -> количество в неотгруженных заказах.
        """
        attempts = [
            {"filter": "shipped=false", "expand": "positions.assortment"},
            {"expand": "positions.assortment"},
            {"expand": "positions.assortment", "limit": 100},
        ]

        for params in attempts:
            try:
                out: dict[str, float] = {}
                for o in self.rows("/entity/customerorder", params=params):
                    if o.get("shipped"):
                        continue
                    if store_name:
                        st = (o.get("store") or {}).get("name", "")
                        if st and st.lower() != store_name.lower():
                            continue
                    for p in (o.get("positions") or {}).get("rows", []):
                        aid = (p.get("assortment") or {}).get("id")
                        if aid:
                            out[aid] = out.get(aid, 0.0) + float(p.get("quantity") or 0)
                if out:
                    log.info("Резерв из заказов: %s позиций", len(out))
                return out
            except Exception as e:  # noqa: BLE001 — запасной источник
                log.warning("Резерв из заказов (%s) не сработал: %s",
                            params.get("filter", "без фильтра"), e)
                continue

        log.warning("Заказы покупателей недоступны — резерв берём из отчёта об остатках")
        return {}

    # --- отгрузки и возвраты --------------------------------------------
    def _since(self, days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def shipped(self, days: int = 28, store_name: str | None = None) -> dict[str, float]:
        """Чистые отгрузки за период, в штуках.

        Продажи — основа всего расчёта. Если этот запрос тихо вернёт пусто,
        дайджест покажет «заказывать нечего», и это будет ложь. Поэтому
        пробуем несколько вариантов фильтра, а если ни один не сработал —
        падаем с понятным сообщением, а не показываем ноль.
        """
        since = self._since(days)
        variants = [
            {"filter": f"moment>={since}", "expand": "positions.assortment,store"},
            {"filter": f"moment>={since}", "expand": "positions.assortment"},
            {"expand": "positions.assortment,store", "limit": 100},
        ]

        last_error = None
        for params in variants:
            out: dict[str, float] = {}
            ok = False
            for path in ("/entity/demand", "/entity/retaildemand"):
                try:
                    for d in self.rows(path, params=params):
                        ok = True
                        if store_name and "store" in str(params.get("expand", "")):
                            st = (d.get("store") or {}).get("name", "")
                            if st and st.lower() != store_name.lower():
                                continue
                        for p in (d.get("positions") or {}).get("rows", []):
                            aid = (p.get("assortment") or {}).get("id")
                            if aid:
                                q = float(p.get("quantity") or 0) - float(p.get("returnedQuantity") or 0)
                                out[aid] = out.get(aid, 0.0) + q
                    break        # retaildemand есть не всегда
                except MoySkladError as e:
                    last_error = e
                    continue
            if ok:
                if not out:
                    log.warning("Отгрузки: запрос прошёл, но продаж за %s дн. нет", days)
                return out

        raise MoySkladError(
            "Не удалось получить отгрузки за %s дн. — расчёт невозможен. "
            "Последняя ошибка: %s" % (days, last_error))

    def returns(self, days: int = 28) -> dict[str, float]:
        out: dict[str, float] = {}
        filt = f"moment>={self._since(days)}"
        for path in ("/entity/salesreturn", "/entity/retailsalesreturn"):
            try:
                for d in self.rows(path, params={"filter": filt,
                                                 "expand": "positions.assortment"}):
                    for p in (d.get("positions") or {}).get("rows", []):
                        aid = (p.get("assortment") or {}).get("id")
                        if aid:
                            out[aid] = out.get(aid, 0.0) + float(p.get("quantity") or 0)
            except MoySkladError:
                continue
        return out

    # --- ожидаемые поступления с датами ---------------------------------
    def arrivals_with_eta(self, now: datetime, store_name: str | None = None):
        arrivals: dict[str, list[dict]] = {}
        first_receipt: dict[str, datetime] = {}

        try:
            for s in self.rows("/entity/supply",
                               params={"expand": "positions.assortment,store"}):
                if store_name:
                    st = (s.get("store") or {}).get("name", "")
                    if st and st.lower() != store_name.lower():
                        continue
                moment = _parse_dt(s.get("moment"))
                if s.get("applicable"):
                    if moment:
                        for p in (s.get("positions") or {}).get("rows", []):
                            aid = (p.get("assortment") or {}).get("id")
                            if aid and (aid not in first_receipt or moment < first_receipt[aid]):
                                first_receipt[aid] = moment
                    continue
                planned = _parse_dt(s.get("plannedMoment")) or moment
                eta = max(0, (planned - now).days) if planned else 0
                for p in (s.get("positions") or {}).get("rows", []):
                    aid = (p.get("assortment") or {}).get("id")
                    qty = float(p.get("quantity") or 0)
                    if aid and qty > 0:
                        arrivals.setdefault(aid, []).append({"eta_days": eta, "qty": qty})
        except MoySkladError:
            pass

        try:
            for o in self.rows("/entity/purchaseorder",
                               params={"expand": "positions.assortment,store"}):
                if o.get("done"):
                    continue
                if store_name:
                    st = (o.get("store") or {}).get("name", "")
                    if st and st.lower() != store_name.lower():
                        continue
                planned = _parse_dt(o.get("deliveryPlannedMoment") or o.get("moment"))
                eta = max(0, (planned - now).days) if planned else 0
                for p in (o.get("positions") or {}).get("rows", []):
                    aid = (p.get("assortment") or {}).get("id")
                    qty = float(p.get("quantity") or 0) - float(p.get("shipped") or 0)
                    if aid and qty > 0:
                        arrivals.setdefault(aid, []).append({"eta_days": eta, "qty": qty})
        except MoySkladError:
            pass

        return arrivals, first_receipt

    # --- поставщики -----------------------------------------------------
    def supplier_map(self, attr_name: str = "Поставщик") -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        try:
            for p in self.rows("/entity/product", params={"expand": "attributes"}):
                v = _attrs(p).get(attr_name)
                if v:
                    out[p["id"]] = (v, "")
            for v in self.rows("/entity/variant",
                               params={"expand": "product.attributes,attributes"}):
                a = _attrs(v).get(attr_name) or _attrs(v.get("product") or {}).get(attr_name)
                if a:
                    out[v["id"]] = (a, "")
        except MoySkladError:
            pass

        for path, filt in (("/entity/supply", None), ("/entity/purchaseorder", None)):
            try:
                for d in self.rows(path, params={"expand": "agent,positions.assortment",
                                                 "order": "moment,desc"}):
                    if path.endswith("supply") and not d.get("applicable"):
                        continue
                    agent = d.get("agent") or {}
                    name, aid_ = agent.get("name", ""), agent.get("id", "")
                    if not name:
                        continue
                    for p in (d.get("positions") or {}).get("rows", []):
                        sku = (p.get("assortment") or {}).get("id")
                        if sku and sku not in out:
                            out[sku] = (name, aid_)
            except MoySkladError:
                continue

        return out


def _barcodes(obj: dict) -> list[str]:
    """Все баркоды сущности: [{"ean13": "..."}, {"gtin": "..."}, ...]."""
    out: list[str] = []
    for b in obj.get("barcodes") or []:
        if isinstance(b, str):
            out.append(b)
            continue
        if not isinstance(b, dict):
            continue
        for k in BARCODE_KEYS:
            v = b.get(k)
            if v:
                out.append(str(v))
    return out


def _attrs(obj: dict) -> dict:
    out = {}
    for a in obj.get("attributes") or []:
        name = a.get("name") or str((a.get("meta") or {}).get("href", "")).rsplit("/", 1)[-1]
        val = a.get("value")
        if isinstance(val, dict):
            val = val.get("name")
        elif isinstance(val, list):
            val = ", ".join(str(x.get("name") if isinstance(x, dict) else x) for x in val)
        if name and val not in (None, ""):
            out[str(name)] = str(val)
    return out


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
