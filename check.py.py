"""Проверка и дайджест — ОДИН файл, без зависимостей от src/.

Запускается как: python check.py
Ничего не импортирует из проекта, поэтому старые файлы в src/
на результат не влияют.
"""
import hashlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

VERSION = "CHECK-4.0"
BASE = "https://api.moysklad.ru/api/remap/1.2"
PAGE_EXPAND = 100          # МойСклад раскрывает positions только при limit<=100


# ---------------------------------------------------------------- утилиты
def say(*a):
    print(*a, flush=True)


def period(days):
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (now - timedelta(days=days)).strftime(fmt), now.strftime(fmt)


def client():
    token = os.environ.get("MOYSKLAD_TOKEN")
    if not token:
        say("НЕТ MOYSKLAD_TOKEN")
        sys.exit(1)
    return httpx.Client(
        base_url=BASE, timeout=90.0,
        headers={"Authorization": f"Bearer {token}",
                 "Accept-Encoding": "gzip",
                 "User-Agent": "stockbot-check/4.0"},
    )


def rows(c, path, params=None, page=1000):
    """Постраничный обход. Возвращает строки, ошибку или бросает."""
    offset, out = 0, []
    while True:
        q = {**(params or {}), "limit": page, "offset": offset}
        r = None
        for attempt in range(4):
            r = c.get(path, params=q)
            if r.status_code == 429:
                time.sleep(3)
                continue
            if r.status_code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            break
        if r.status_code == 401:
            raise RuntimeError("401: токен невалиден")
        if r.status_code == 403:
            raise RuntimeError(f"403: нет прав на {path}")
        if r.status_code == 404:
            raise RuntimeError(f"404: {path} недоступен")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} на {path}: {r.text[:150]}")

        data = r.json()
        chunk = data.get("rows") or []
        out.extend(chunk)
        offset += page
        total = (data.get("meta") or {}).get("size")
        if not chunk or len(chunk) < page:
            break
        if total is not None and offset >= total:
            break
    return out


def qty_of(row):
    """Количество проданного: разные отчёты кладут в разные поля."""
    for path in (("outcome", "quantity"), ("sellQuantity",), ("quantity",)):
        cur = row
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, (int, float)):
            return float(cur)
    return 0.0


# ---------------------------------------------------------------- проверки
def check_reports(c, days):
    since, until = period(days)
    variants = [
        ("dateFrom/dateTo  +groupBy", {"dateFrom": since, "dateTo": until, "groupBy": "product"}),
        ("dateFrom/dateTo",           {"dateFrom": since, "dateTo": until}),
        ("momentFrom/momentTo +groupBy", {"momentFrom": since, "momentTo": until, "groupBy": "product"}),
        ("momentFrom/momentTo",       {"momentFrom": since, "momentTo": until}),
        ("без дат +groupBy",          {"groupBy": "product"}),
        ("без дат",                   {}),
    ]
    best = {}
    for label, params in variants:
        try:
            rs = rows(c, "/report/turnover/all", params, page=100)
        except Exception as e:                                  # noqa: BLE001
            say(f"  обороты [{label:28}] ошибка: {e}")
            continue
        if not rs:
            say(f"  обороты [{label:28}] пусто")
            continue
        total = sum(qty_of(r) for r in rs)
        keys = ", ".join(list(rs[0])[:8])
        say(f"  обороты [{label:28}] строк {len(rs):>4} · штук {total:>7.0f} · поля: {keys}")
        if total > 0 and not best:
            best = {r["assortment"]["id"]: qty_of(r) for r in rs
                    if (r.get("assortment") or {}).get("id")}
    return best


def check_docs(c, days):
    since, _ = period(days)
    found = {}
    for path in ("/entity/demand", "/entity/retaildemand"):
        try:
            docs = rows(c, path,
                        {"filter": f"moment>={since}",
                         "expand": "positions.assortment"},
                        page=PAGE_EXPAND)
        except Exception as e:                                  # noqa: BLE001
            say(f"  {path:22} ошибка: {e}")
            continue
        if not docs:
            say(f"  {path:22} документов нет")
            continue
        with_rows = sum(1 for d in docs
                        if isinstance(d.get("positions"), dict)
                        and d["positions"].get("rows"))
        total = sum(float(p.get("quantity") or 0)
                    for d in docs
                    if isinstance(d.get("positions"), dict)
                    for p in d["positions"].get("rows") or [])
        say(f"  {path:22} документов {len(docs):>4} · с позициями в ответе "
            f"{with_rows:>4} · штук {total:>7.0f}")
        for d in docs:
            pos = d.get("positions")
            if not isinstance(pos, dict):
                continue
            for p in pos.get("rows") or []:
                aid = (p.get("assortment") or {}).get("id")
                if aid:
                    q = float(p.get("quantity") or 0) - float(p.get("returnedQuantity") or 0)
                    found[aid] = found.get(aid, 0.0) + q
    return found


# ---------------------------------------------------------------- main
def main():
    say("=" * 68)
    say(f"  {VERSION}")
    h = hashlib.md5(open(__file__, "rb").read()).hexdigest()[:10]
    say(f"  check.py  {h}")
    say("=" * 68)

    days = int(os.environ.get("WINDOW_DAYS", "28"))
    c = client()

    say("\n[1] СКЛАДЫ")
    stores = {s["id"]: s.get("name", "") for s in rows(c, "/entity/store")}
    for sid, name in stores.items():
        say(f"  {name}   ({sid})")

    want = os.environ.get("WAREHOUSE", "Основной склад").strip().lower()
    sid = next((i for i, n in stores.items() if n.strip().lower() == want), None)
    if sid is None:
        sid = next((i for i, n in stores.items() if want in n.lower()), None)
    if sid is None:
        say(f"  ! склад «{want}» не найден")
        return
    say(f"  → выбран: {stores[sid]}")

    say(f"\n[2] ОСТАТКИ (за {days} дн. продажи, склад для доступного)")
    href = f"{BASE}/entity/store/{sid}"
    t0 = time.monotonic()
    try:
        st = rows(c, "/report/stock/all", {"filter": f"store={href}"}, page=1000)
        say(f"  /report/stock/all: {len(st)} позиций  ({time.monotonic()-t0:.1f} c)")
        if st:
            keys = ", ".join(list(st[0])[:9])
            say(f"  поля: {keys}")
    except Exception as e:                                      # noqa: BLE001
        say(f"  /report/stock/all: {e}")

    say(f"\n[3] ПРОДАЖИ — ОТЧЁТЫ")
    t0 = time.monotonic()
    from_report = check_reports(c, days)
    say(f"  → из отчёта получено позиций: {len(from_report)}  "
        f"({time.monotonic()-t0:.1f} c)")

    say(f"\n[4] ПРОДАЖИ — ДОКУМЕНТЫ (limit={PAGE_EXPAND} + expand)")
    t0 = time.monotonic()
    from_docs = check_docs(c, days)
    say(f"  → из документов получено позиций: {len(from_docs)}  "
        f"({time.monotonic()-t0:.1f} c)")

    say("\n" + "=" * 68)
    if from_report:
        say(f"  ВЫВОД: отчёт работает → {len(from_report)} позиций. "
            f"Использовать его.")
    elif from_docs:
        say(f"  ВЫВОД: отчёт пуст, документы работают → {len(from_docs)} позиций. "
            f"Использовать документы.")
    else:
        say("  ВЫВОД: данных о продажах нет ни там, ни там.")
    say("=" * 68)


if __name__ == "__main__":
    main()
