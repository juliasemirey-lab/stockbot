"""Сборка данных: источники -> строки для расчётного ядра."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from ms_client import MS, MoySkladError
import wb_client

log = logging.getLogger("stockbot.pipeline")


def classify_folder(folder: str, folder_map: dict, exclude: list[str]) -> str | None:
    fl = (folder or "").lower()
    if any((e or "").lower() in fl for e in exclude or []):
        return None
    best, best_len = None, -1
    for key, cat in folder_map.items():
        if key.lower() in fl and len(key) > best_len:
            best, best_len = cat, len(key)
    return best or "blouses"


def days_in_stock_proxy(sku_id: str, window: int, first_receipt: dict[str, datetime]) -> float:
    t = first_receipt.get(sku_id)
    if t is None:
        return float(window)
    d = (datetime.now(timezone.utc) - t).days
    return float(max(1, min(window, d)))


def _wb_index(cfg: dict) -> dict[str, dict]:
    wb_cfg = cfg.get("wildberries") or {}
    if not wb_cfg.get("enabled"):
        return {}
    try:
        return wb_client.build_index_with_cache(
            token=None,
            cache_path=os.environ.get("WB_CACHE_PATH")
            or wb_cfg.get("cache_path", "/tmp/wb_barcode_index.json"),
            ttl_minutes=int(wb_cfg.get("cache_ttl_minutes", 720)),
        )
    except wb_client.WBError as e:
        log.warning("WB недоступен, ссылки не будут проставлены: %s", e)
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning("WB: непредвиденная ошибка: %s", e)
        return {}


def _wb_links(barcodes: list[str], article: str, index: dict[str, dict],
              cfg: dict) -> tuple[dict, dict, str]:
    """Баркоды -> ссылка на карточку WB. Возвращает (links, kinds, barcode_used)."""
    wb_cfg = cfg.get("wildberries") or {}
    links: dict[str, str] = {}
    kinds: dict[str, str] = {}

    for bc in barcodes:
        hit = wb_client.resolve(bc, index)
        if hit:
            links["wb"] = hit["url"]
            kinds["wb"] = "card"
            return links, kinds, bc

    if wb_cfg.get("search_fallback") and article:
        tpl = wb_cfg.get("search_url") or "https://www.wildberries.ru/catalog/0/search.aspx?search={q}"
        links["wb"] = tpl.format(q=quote_plus(article))
        kinds["wb"] = "search"
    return links, kinds, ""


def build_rows_from_ms(ms: MS, cfg: dict, with_links: bool = True) -> list[dict]:
    window = int(cfg.get("velocity_window_days", 28))
    sup_attr = cfg.get("supplier_attribute") or "Поставщик"
    store = cfg.get("warehouse") or None
    now = datetime.now(timezone.utc)

    meta = ms.assortment_meta()
    stock = ms.stock(store)                       # только нужный склад
    shipped = ms.shipped(window, store)
    returns = ms.returns(window)
    reserve_acc = ms.reserve_from_orders(store)
    arrivals_by_sku, first_receipt = ms.arrivals_with_eta(now, store)
    supplier_by_sku = ms.supplier_map(sup_attr)
    wb_index = _wb_index(cfg) if with_links else {}

    rows = []
    for aid, info in meta.items():
        cat = classify_folder(info["folder"], cfg.get("folder_map", {}),
                              cfg.get("exclude_folders", []))
        if cat is None:
            continue

        st = stock.get(aid, {"stock": 0.0, "reserve": 0.0})
        attrs = info.get("attrs") or {}
        article = info.get("article", "") or attrs.get("Артикул", "")
        barcodes = info.get("barcodes") or []
        supplier, supplier_id = supplier_by_sku.get(aid, ("", ""))
        if not supplier:
            supplier = attrs.get(sup_attr, "")

        links, kinds, bc_used = _wb_links(barcodes, article, wb_index, cfg)

        rows.append({
            "sku_id": aid,
            "article": article,
            "name": info.get("name", ""),
            "color": info.get("color", ""),
            "size": info.get("size", ""),
            "category": cat,
            "supplier": supplier,
            "supplier_id": supplier_id,
            "stock": st["stock"],
            "reserve": st["reserve"] or reserve_acc.get(aid, 0.0),
            "in_transit": sum(a["qty"] for a in arrivals_by_sku.get(aid, [])),
            "arrivals": arrivals_by_sku.get(aid, []),
            "units_net": max(0.0, shipped.get(aid, 0.0) - returns.get(aid, 0.0)),
            "days_in_stock": days_in_stock_proxy(aid, window, first_receipt),
            "barcodes": barcodes,
            "barcode_matched": bc_used,
            "links": links,
            "link_kind": kinds,
        })
    return rows


def build_rows_from_dump(path: str, cfg: dict, with_links: bool = True) -> list[dict]:
    """Офлайн-режим: JSON того же вида, что отдаёт build_rows_from_ms."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    window = int(cfg.get("velocity_window_days", 28))
    has_wb = (cfg.get("wildberries") or {}).get("enabled")
    wb_index = _wb_index(cfg) if (has_wb and with_links) else {}

    for r in data:
        r.setdefault("days_in_stock", window)
        r.setdefault("reserve", 0.0)
        r.setdefault("in_transit", 0.0)
        r.setdefault("arrivals", [])
        r.setdefault("supplier", "")
        r.setdefault("supplier_id", "")
        r.setdefault("barcodes", [])
        # ссылки не берём из дампа — они восстанавливаются по баркоду,
        # иначе офлайн-прогон не проверяет реальное сопоставление
        links, kinds, bc = _wb_links(r.get("barcodes") or [],
                                     r.get("article", ""), wb_index, cfg)
        r["links"], r["link_kind"], r["barcode_matched"] = links, kinds, bc

    return data
