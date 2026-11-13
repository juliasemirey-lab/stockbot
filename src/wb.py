"""Wildberries: баркод -> ссылка на карточку.

Нужен токен с категорией «Контент». Если токена нет — работаем без ссылок,
дайджест от этого не ломается.
"""
from __future__ import annotations

import logging
import os

import httpx

CARDS_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
CARD_URL = "https://www.wildberries.ru/catalog/{nmid}/detail.aspx"
SEARCH_URL = "https://www.wildberries.ru/catalog/0/search.aspx?search={q}"

log = logging.getLogger("stockbot.wb")


class WBError(RuntimeError):
    pass


def normalize(code: str) -> str:
    """Оставляет только цифры: баркоды приходят с пробелами и дефисами."""
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def variants(code: str) -> list[str]:
    """Варианты записи баркода: с ведущими нулями и без."""
    c = normalize(code)
    if not c:
        return []
    out = [c]
    stripped = c.lstrip("0")
    if stripped and stripped != c:
        out.append(stripped)
    if len(c) < 13:
        out.append(c.zfill(13))
    return out


def index(token: str | None = None) -> dict[str, str]:
    """Баркод -> ссылка на карточку WB."""
    token = token or os.environ.get("WB_TOKEN")
    if not token:
        log.warning("Нет WB_TOKEN — ссылки WB не будут проставлены")
        return {}

    out: dict[str, str] = {}
    cards = 0
    cursor = {"limit": 100}

    with httpx.Client(timeout=60.0) as c:
        for _ in range(300):                     # предохранитель от бесконечного цикла
            r = c.post(CARDS_URL, json={"settings": {"cursor": cursor, "filter": {}}},
                       headers={"Authorization": token})
            if r.status_code == 401:
                log.warning("WB: токен не принят (401)")
                return out
            if r.status_code == 403:
                log.warning("WB: у токена нет категории «Контент» (403)")
                return out
            if r.status_code >= 400:
                raise WBError(f"WB cards: HTTP {r.status_code} {r.text[:150]}")

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
                        if code:
                            out.setdefault(code, url)

            nxt = (data.get("cursor") or {}).get("updatedAt")
            if not batch or not nxt or nxt == cursor.get("updatedAt"):
                break
            cursor = {"limit": 100, "updatedAt": nxt}

    log.info("WB: карточек %s, баркодов %s", cards, len(out))
    return out


def link(barcodes: list[str], article: str, index_map: dict[str, str],
         search_fallback: bool = True) -> str:
    """Ссылка по баркоду; если не нашлось — поиск по артикулу."""
    for bc in barcodes or []:
        for v in variants(bc):
            hit = index_map.get(v)
            if hit:
                return hit

    if search_fallback and article:
        from urllib.parse import quote_plus
        return SEARCH_URL.format(q=quote_plus(str(article)))
    return ""
