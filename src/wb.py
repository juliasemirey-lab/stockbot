"""Wildberries: баркод -> ссылка на карточку.

Сопоставление строго по баркоду, без артикула и без догадок.
Баркоды в МойСклад и на WB встречаются длиной 13 и 14 знаков,
поэтому длина проверяется, а ведущие нули не трогаются:
0001234567890 и 1234567890 — разные коды.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

CARDS_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
CARD_URL = "https://www.wildberries.ru/catalog/{nmid}/detail.aspx"

VALID_LENGTHS = (13, 14)

log = logging.getLogger("stockbot.wb")


class WBError(RuntimeError):
    pass


def normalize(code: str) -> str:
    """Только цифры. Никаких обрезаний и добиваний."""
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def valid(code: str) -> bool:
    return len(code) in VALID_LENGTHS


def _cache_path() -> Path:
    return Path(os.environ.get("WB_CACHE_PATH", ".wb_cache.json"))


def _load_cache(ttl_days: int) -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return None
    age = time.time() - float(data.get("ts") or 0)
    if age > ttl_days * 86400:
        log.info("WB: кэш устарел (%.1f дн.)", age / 86400)
        return None
    log.info("WB: кэш использован · баркодов %s · возраст %.1f дн.",
             len(data.get("index") or {}), age / 86400)
    return data


def _save_cache(index: dict[str, str], conflicts: list[str]) -> None:
    try:
        _cache_path().write_text(
            json.dumps({"ts": time.time(), "index": index,
                        "conflicts": conflicts}, ensure_ascii=False),
            encoding="utf-8")
        log.info("WB: кэш сохранён · баркодов %s", len(index))
    except Exception as e:                                       # noqa: BLE001
        log.warning("WB: кэш не сохранён: %s", e)


def fetch(token: str | None = None) -> tuple[dict[str, str], set[str]]:
    """Обход карточек WB. Возвращает (баркод->ссылка, конфликтные).

    Курсор следующей страницы требует ОБА значения: updatedAt и nmID.
    Если передать только время, обход останавливается после первой
    сотни карточек — и половина каталога остаётся невидимой.
    """
    token = token or os.environ.get("WB_TOKEN")
    if not token:
        log.warning("Нет WB_TOKEN — ссылки WB не будут проставлены")
        return {}, set()

    seen: dict[str, set[str]] = {}
    cards = 0
    cursor: dict = {"limit": 100}

    with httpx.Client(timeout=90.0) as c:
        for _ in range(500):
            r = c.post(CARDS_URL,
                       json={"settings": {"cursor": cursor, "filter": {}}},
                       headers={"Authorization": token})
            if r.status_code in (401, 403):
                log.warning("WB: токен не принят (%s) — проверьте категорию "
                            "«Контент» и что токен из того же кабинета",
                            r.status_code)
                return {}, set()
            if r.status_code >= 400:
                raise WBError(f"WB cards: HTTP {r.status_code} {r.text[:200]}")

            data = r.json()
            batch = data.get("cards") or []
            for card in batch:
                cards += 1
                nmid = card.get("nmID") or card.get("nmId")
                if not nmid:
                    continue
                url = CARD_URL.format(nmid=nmid)
                for size in card.get("sizes") or []:
                    for sku in size.get("skus") or []:
                        code = normalize(sku)
                        if valid(code):
                            seen.setdefault(code, set()).add(url)

            nxt = data.get("cursor") or {}
            updated = nxt.get("updatedAt")
            nm = nxt.get("nmID") or nxt.get("nmId")
            if not batch or not updated:
                break
            new_cursor = {"limit": 100, "updatedAt": updated}
            if nm:
                new_cursor["nmID"] = nm
            if new_cursor == cursor:
                break
            cursor = new_cursor

    index: dict[str, str] = {}
    conflicts: set[str] = set()
    for code, urls in seen.items():
        if len(urls) == 1:
            index[code] = next(iter(urls))
        else:
            conflicts.add(code)

    log.info("WB: карточек %s · баркодов %s · конфликтных %s",
             cards, len(index), len(conflicts))
    return index, conflicts


def index(token: str | None = None,
          ttl_days: int = 3) -> tuple[dict[str, str], set[str]]:
    """Индекс с кэшем: свежий берём с диска, иначе качаем заново."""
    cached = _load_cache(ttl_days)
    if cached is not None:
        return (cached.get("index") or {},
                set(cached.get("conflicts") or []))

    index_map, conflicts = fetch(token)
    if index_map:
        _save_cache(index_map, sorted(conflicts))
    return index_map, conflicts


def match(barcodes: list[str], index_map: dict[str, str],
          conflicts: set[str]) -> tuple[str, str]:
    """Ищет ссылку по всем баркодам товара.

    Возвращает (url, как): "13", "14", "conflict", "none".
    """
    found: dict[str, str] = {}
    for bc in barcodes:
        code = normalize(bc)
        if not valid(code):
            continue
        if code in conflicts:
            return "", "conflict"
        hit = index_map.get(code)
        if hit:
            found[code] = hit

    if not found:
        return "", "none"

    urls = set(found.values())
    if len(urls) > 1:
        # два баркода одного товара ведут на разные карточки —
        # один из баркодов в МойСклад от другого товара
        return "", "split"

    url = next(iter(urls))
    length = "13" if any(len(c) == 13 for c in found) else "14"
    return url, length
