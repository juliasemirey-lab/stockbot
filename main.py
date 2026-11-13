"""Дайджест пополнения склада.

Режимы:
  dry-run   посчитать и напечатать, без Telegram
  once      отправить дайджест в Telegram и выйти (для GitHub Actions)

Источники данных — два отчёта МойСклад:
  /report/stock/bystore    остаток и резерв основного склада
  /report/turnover/all     продажи за период по всем складам
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))

from calc import build, to_order                                    # noqa: E402
from render import csv_bytes, digest                               # noqa: E402

VERSION = "3.0"

log = logging.getLogger("stockbot")


def load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    local = Path(path).with_name("config.local.yaml")
    if local.exists():
        with open(local, encoding="utf-8") as f:
            cfg.update(yaml.safe_load(f) or {})
        log.info("Применены локальные настройки: %s", local)
    return cfg


def collect(cfg: dict, debug: bool = False):
    """Возвращает список позиций к заказу."""
    from ms import MS

    ms = MS()
    store_id = ms.resolve_store(cfg["warehouse"])
    items = ms.assortment(cfg.get("exclude_folders") or [])
    stock = ms.stock(store_id)
    sales = ms.sales(int(cfg.get("velocity_window_days", 28)), debug=debug)

    items = build(items, stock, sales, cfg, date.today())

    if (cfg.get("wildberries") or {}).get("enabled"):
        import wb
        idx = wb.index()
        fallback = bool((cfg.get("wildberries") or {}).get("search_fallback", True))
        found = 0
        for it in items:
            it.wb_url = wb.link(it.barcodes, it.article, idx, fallback)
            if it.wb_url and it.barcodes:
                found += 1
        log.info("Ссылки WB: %s из %s по баркоду", found, len(items))

    profiles: dict[str, int] = {}
    for it in items:
        profiles[it.profile] = profiles.get(it.profile, 0) + 1
    log.info("Профили: %s", ", ".join(f"{k}={v}" for k, v in
                                      sorted(profiles.items(), key=lambda x: -x[1])))

    return to_order(items)


def cmd_dry(args):
    cfg = load_cfg(args.config)
    need = collect(cfg, debug=args.debug)
    print(digest(need, top_n=int(cfg.get("top_n_in_chat", 15))))
    if args.csv:
        Path(args.csv).write_bytes(csv_bytes(need))
        print(f"\nCSV: {args.csv} ({len(need)} строк)", file=sys.stderr)


def cmd_sources(args):
    """Показывает, что реально вернул каждый источник продаж."""
    from ms import MS

    cfg = load_cfg(args.config)
    ms = MS()
    days = int(cfg.get("velocity_window_days", 28))

    print(f"Проверка источников продаж за {days} дн.\n")
    for name, result in ms.probe(days):
        print(f"  {name}")
        print(f"      {result}\n")

    print("Если у документов «с позициями» = 0 — раскрытие не работает,")
    print("и продажи берутся поштучной догрузкой (медленно).\n")

    store_id = ms.resolve_store(cfg["warehouse"])
    stock = ms.stock(store_id)
    print(f"Остаток на складе «{cfg['warehouse']}»: {len(stock)} позиций")

    sales = ms.sales(days)
    print(f"Продажи, которые пойдут в расчёт: {len(sales)} позиций, "
          f"{sum(sales.values()):.0f} шт.")


def cmd_once(args):
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import FSInputFile

    cfg = load_cfg(args.config)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Нужен TELEGRAM_BOT_TOKEN")
    chats = [int(x) for x in (os.environ.get("TELEGRAM_CHAT_IDS") or "").split(",") if x.strip()]
    if not chats:
        sys.exit("Нужен TELEGRAM_CHAT_IDS")

    need = collect(cfg)
    text = digest(need, top_n=int(cfg.get("top_n_in_chat", 15)))
    path = Path(f"digest_{date.today():%Y%m%d}.csv")
    path.write_bytes(csv_bytes(need))

    async def send():
        bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
        try:
            for cid in chats:
                await bot.send_message(cid, text, disable_web_page_preview=True)
                if need:
                    await bot.send_document(
                        cid, FSInputFile(str(path)),
                        caption=f"{len(need)} позиций")
        finally:
            await bot.session.close()

    asyncio.run(send())
    log.info("Отправлено: %s позиций", len(need))


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Отпечаток кода: по нему видно, какая версия реально запущена.
    # Если в логе нет строки «ВЕРСИЯ 3.0» — значит на GitHub старые файлы.
    import hashlib
    here = Path(__file__).parent
    print("=" * 60)
    print(f"  ВЕРСИЯ {VERSION}")
    for name in ("main.py", "src/ms.py", "src/calc.py"):
        f = here / name
        if f.exists():
            h = hashlib.md5(f.read_bytes()).hexdigest()[:10]
            print(f"  {name:14} {h}")
    print("=" * 60)
    p = argparse.ArgumentParser(description="Дайджест пополнения склада")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dry-run", help="посчитать и напечатать")
    d.add_argument("--csv", default="digest.csv")
    d.add_argument("--debug", action="store_true",
                   help="показать структуру строки отчёта по продажам")
    d.set_defaults(func=cmd_dry)

    o = sub.add_parser("once", help="отправить в Telegram и выйти")
    o.set_defaults(func=cmd_once)

    src = sub.add_parser("sources", help="что реально вернул каждый источник")
    src.set_defaults(func=cmd_sources)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
