"""Формирование черновика «Заказа поставщику» в МойСклад.

Документ создаётся НЕ проведённым (applicable=false): его проверяет и
проводит человек. Автоматическая проводка закупок без ревью — плохая идея.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

from calc import Line

BASE = "https://api.moysklad.ru/api/remap/1.2"


class OrderError(RuntimeError):
    pass


def _client() -> httpx.Client:
    token = os.environ.get("MOYSKLAD_TOKEN")
    if not token:
        raise OrderError("Нет MOYSKLAD_TOKEN — создать заказ можно только с доступом к API.")
    return httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=60,
    )


def _single(c: httpx.Client, path: str, what: str, cfg: dict, key: str) -> dict:
    href = cfg.get(key)
    if href:
        return {"meta": {"href": href, "mediaType": "application/json"}}
    r = c.get(path, params={"limit": 2})
    rows = r.json().get("rows", [])
    if not rows:
        raise OrderError(f"В аккаунте нет сущности «{what}» — заказ создать нельзя.")
    if len(rows) > 1:
        raise OrderError(f"Несколько {what}: укажите {key} в config.yaml.")
    return rows[0]["meta"]


def build_payload(lines: list[Line], org: dict, store: dict) -> dict:
    positions = []
    for ln in lines:
        if ln.need <= 0 or not ln.sku_id:
            continue
        positions.append({
            "quantity": int(ln.need),
            "price": 0,            # цена подставится из карточки/договора
            "vat": 0,
            "assortment": {
                "meta": {
                    "href": f"{BASE}/entity/assortment/{ln.sku_id}",
                    "type": "variant" if (ln.size or ln.color) else "product",
                    "mediaType": "application/json",
                }
            },
        })

    # плановая дата поставки: к началу расчётного окна
    target = lines[0].window_start if lines and lines[0].window_start else None
    if target is None:
        target = (datetime.now(timezone.utc) + timedelta(
            days=int(lines[0].arrival_days if lines else 3))).date()

    return {
        "name": f"Автозаказ {datetime.now():%d.%m.%Y}",
        "organization": org,
        "store": store,
        "applicable": False,
        "deliveryPlannedMoment": f"{target:%Y-%m-%d} 12:00:00",
        "description": "Сформировано ботом по дайджесту остатков. Проверьте перед проводкой.",
        "positions": positions,
    }


def create_order(lines: list[Line], cfg: dict) -> tuple[str, int, str]:
    """Возвращает (номер документа, позиций, ссылка на документ)."""
    if not lines:
        raise OrderError("Нечего заказывать: нет позиций с потребностью.")

    with _client() as c:
        org = _single(c, "/entity/organization", "организаций",
                      cfg, "order_organization_href")
        store = _single(c, "/entity/store", "складов", cfg, "order_store_href")

        payload = build_payload(lines, org, store)
        if not payload["positions"]:
            raise OrderError("Нет ни одной позиции с положительной потребностью.")

        if lines[0].supplier_id:
            payload["agent"] = {
                "meta": {"href": f"{BASE}/entity/counterparty/{lines[0].supplier_id}",
                         "type": "counterparty", "mediaType": "application/json"}
            }

        r = c.post("/entity/purchaseorder", json=payload)
        if r.status_code == 409:
            raise OrderError("МойСклад отклонил документ (409). Проверьте права токена.")
        if r.status_code == 412:
            raise OrderError(f"Не хватает обязательных полей: {r.text[:300]}")
        r.raise_for_status()
        d = r.json()

    name = d.get("name", "")
    link = f"https://online.moysklad.ru/app/#purchaseorder/edit?id={d.get('id', '')}"
    return name, len(payload["positions"]), link
