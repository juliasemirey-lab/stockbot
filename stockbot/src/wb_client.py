"""Клиент Wildberries Content API: карточки -> nmID по баркоду.

Ключевое: в карточке WB баркоды лежат в sizes[].skus[], а не отдельным полем.
Одна карточка (nmID) может содержать несколько размеров, у каждого свой
баркод — поэтому соответствие строится на уровне баркода, а не карточки.

Ссылка на карточку: https://www.wildberries.ru/catalog/{nmID}/detail.aspx
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Iterable

import httpx

log = logging.getLogger("stockbot.wb")

CONTENT_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
CARD_URL = "https://www.wildberries.ru/catalog/{nmid}/detail.aspx"


class WBError(RuntimeError):
    pass


def normalize_barcode(raw) -> str:
    """Приводит баркод к сравнимому виду: только цифры, без ведущих нулей.

    EAN-13 и GTIN-14 одного товара отличаются ведущим нулём, а WB и МойСклад
    могут хранить разные длины — без нормализации сопоставление не сойдётся.
    """
    if raw is None:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    return digits.lstrip("0") or ""


def variants(code: str) -> set[str]:
    """Возможные записи одного баркода: как есть, с нулями до 13 и 14 знаков."""
    if not code:
        return set()
    out = {code, code.zfill(13), code.zfill(14)}
    return {c for c in out if c}


class WB:
    def __init__(self, token: str | None = None, timeout: float = 60.0):
        token = token or os.environ.get("WB_TOKEN")
        if not token:
            raise WBError("Нет WB_TOKEN: задайте переменную окружения.")
        self.c = httpx.Client(
            headers={"Authorization": token, "Content-Type": "application/json"},
            timeout=timeout,
        )

    def cards(self, page_limit: int = 100, max_pages: int = 200) -> Iterable[dict]:
        """Курсорная пагинация: updatedAt + nmID как позиция в курсоре."""
        cursor: dict = {"limit": page_limit}
        for _ in range(max_pages):
            body = {
                "settings": {
                    "cursor": cursor,
                    "filter": {"withPhoto": -1},
                }
            }
            r = self._post(body)
            data = r.json()
            cards = data.get("cards") or []
            yield from cards
            cur = data.get("cursor") or {}
            total = int(cur.get("total") or 0)
            updated = cur.get("updatedAt")
            nmid = cur.get("nmID")
            # WB отдаёт total < limit на последней странице
            if not cards or total < page_limit or not updated:
                break
            cursor = {"limit": page_limit, "updatedAt": updated, "nmID": nmid}
        else:
            log.warning("WB: достигнут лимит страниц (%s), возможно не всё выгружено", max_pages)

    def _post(self, body: dict) -> httpx.Response:
        for attempt in range(6):
            r = self.c.post(CONTENT_URL, json=body)
            if r.status_code == 429:
                wait = int(r.headers.get("X-Ratelimit-Retry", 2))
                time.sleep(max(wait, 1))
                continue
            if r.status_code == 401:
                raise WBError("401: WB-токен невалиден или истёк")
            if r.status_code == 403:
                raise WBError("403: у WB-токена нет категории «Контент»")
            if r.status_code >= 500:
                if attempt == 5:
                    raise WBError(f"WB: HTTP {r.status_code}")
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        raise WBError("WB: не удалось получить ответ после повторов")

    def barcode_index(self) -> dict[str, dict]:
        """Баркод (нормализованный) -> {nmid, url, vendor_code, size_name}."""
        index: dict[str, dict] = {}
        cards = 0
        for card in self.cards():
            cards += 1
            nmid = card.get("nmID") or card.get("nmId")
            if not nmid:
                continue
            url = CARD_URL.format(nmid=nmid)
            vendor = card.get("vendorCode", "")
            for size in card.get("sizes") or []:
                size_name = size.get("techSize") or size.get("tech_size") or ""
                for sku in size.get("skus") or []:
                    code = normalize_barcode(sku)
                    if not code:
                        continue
                    # первый встреченный выигрывает: дубли баркодов на WB — ошибка данных
                    index.setdefault(code, {
                        "nmid": nmid,
                        "url": url,
                        "vendor_code": vendor,
                        "size_name": size_name,
                    })
        log.info("WB: карточек %s, баркодов %s", cards, len(index))
        return index


def resolve(barcode: str, index: dict[str, dict]) -> dict | None:
    """Ищет карточку по любому варианту записи баркода."""
    for v in variants(normalize_barcode(barcode)):
        hit = index.get(v)
        if hit:
            return hit
    return None


def build_index_with_cache(token: str | None, cache_path: str,
                           ttl_minutes: int = 720) -> dict[str, dict]:
    """Индекс баркодов с кэшем на диск.

    Полная выгрузка карточек WB — это десятки запросов и жёсткие лимиты
    частоты. Держим результат в файле, чтобы недельный дайджест не
    выгружал всё заново каждый запуск.
    """
    import json
    from pathlib import Path

    p = Path(cache_path)
    if p.exists():
        age_min = (time.time() - p.stat().st_mtime) / 60
        if age_min < ttl_minutes:
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                log.info("WB: индекс из кэша (%s баркодов, %.0f мин назад)",
                         len(raw), age_min)
                return raw
            except (json.JSONDecodeError, OSError):
                pass

    index = WB(token).barcode_index()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("WB: не удалось сохранить кэш: %s", e)
    return index
