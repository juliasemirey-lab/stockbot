"""Дайджест пополнения склада.

Режимы:
  dry-run   посчитать и напечатать в консоль
  once      отправить в Telegram и выйти (для GitHub Actions)
  sources   диагностика: что реально вернул каждый источник

Данные:
  расход   — нетто-отток С ОСНОВНОГО СКЛАДА за окно: отгрузки, розничные
             продажи, перемещения на буферы и Ярмарку, минус возвраты
             покупателей и перемещения обратно на основной.
             Отгрузки с буферов и с Ярмарки не считаются: товар ушёл не
             с того склада, который мы наполняем
  остатки  — основной склад (показываем и считаем) + «Ярмарки» (только считаем:
             товар наш, но физически не на полке и в маркетплейс не выгружен)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))

from calc import build, make_groups_full, split_tiers          # noqa: E402
from render import csv_bytes, digest                           # noqa: E402

VERSION = "6.2"

log = logging.getLogger("stockbot")


def banner() -> None:
    """Отпечаток: по нему видно, какая версия реально запущена."""
    here = Path(__file__).parent
    print("=" * 60)
    print(f"  ВЕРСИЯ {VERSION}")
    for name in ("main.py", "src/ms.py", "src/calc.py",
                 "src/render.py", "src/wb.py"):
        f = here / name
        if f.exists():
            h = hashlib.md5(f.read_bytes()).hexdigest()[:10]
            print(f"  {name:16} {h}")
    print("=" * 60)


def load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def collect(cfg: dict, debug: bool = False):
    from ms import MS

    ms = MS()
    main_store = cfg["warehouse"]
    yard_store = (cfg.get("yard_warehouse") or "").strip()

    names = [main_store] + ([yard_store] if yard_store else [])
    stocks = ms.store_stock(names, required=main_store)
    main_stock = stocks.get(main_store, {})
    yard_stock = stocks.get(yard_store, {}) if yard_store else {}

    items = ms.assortment(cfg.get("exclude_folders") or [])
    sales = ms.sales(int(cfg.get("velocity_window_days", 28)), main_store)
    suppliers = ms.supplier_names()

    built = build(items, main_stock, sales, suppliers, cfg, date.today(),
                  yard=yard_stock)

    wb_cfg = cfg.get("wildberries") or {}
    if wb_cfg.get("enabled"):
        import wb
        idx, conflicts = wb.index(ttl_days=int(wb_cfg.get("cache_days", 3)))
        stats = {"13": 0, "14": 0, "conflict": 0, "split": 0, "none": 0}
        for it in built:
            url, how = wb.match(it.barcodes, idx, conflicts)
            it.wb_url = url
            stats[how] = stats.get(how, 0) + 1
        log.info("Ссылки WB: по 13 %s · по 14 %s · конфликт %s · "
                 "разные карточки %s · без ссылки %s",
                 stats["13"], stats["14"], stats["conflict"],
                 stats["split"], stats["none"])

    return built


def _tiers(cfg: dict, built):
    """Делит каталог на горящее и остальное.

    Группы строятся по ВСЕМУ каталогу, а не только по позициям к заказу:
    иначе «доля вымытой сетки» всегда равна единице.
    """
    groups_all = make_groups_full(built)
    groups_need = [g for g in groups_all if g.total_order > 0]
    return split_tiers(groups_need, cfg)


def cmd_dry(args):
    cfg = load_cfg(args.config)
    built = collect(cfg, debug=args.debug)
    hot, rest = _tiers(cfg, built)
    print(digest(hot, rest, date.today(),
                 horizon=int(cfg.get("horizon_days", 7))))
    if args.csv:
        Path(args.csv).write_bytes(csv_bytes(hot, rest))
        print(f"\nCSV: {args.csv} (горит {len(hot)}, в файле {len(rest)})",
              file=sys.stderr)


def cmd_sources(args):
    from ms import MS

    cfg = load_cfg(args.config)
    ms = MS()
    days = int(cfg.get("velocity_window_days", 28))

    print(f"Проверка источников за {days} дн.\n")
    for name, result in ms.probe(days, cfg.get("warehouse", "")):
        print(f"  {name}")
        print(f"      {result}\n")


def cmd_once(args):
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import FSInputFile

    cfg = load_cfg(args.config)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Нужен TELEGRAM_BOT_TOKEN")
    chats = [int(x) for x in (os.environ.get("TELEGRAM_CHAT_IDS") or "").split(",")
             if x.strip()]
    if not chats:
        sys.exit("Нужен TELEGRAM_CHAT_IDS")

    built = collect(cfg)
    hot, rest = _tiers(cfg, built)
    text = digest(hot, rest, date.today(),
                  horizon=int(cfg.get("horizon_days", 7)))
    path = Path(f"digest_{date.today():%Y%m%d}.csv")
    path.write_bytes(csv_bytes(hot, rest))
    total = len(hot) + len(rest)

    async def send():
        bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            for cid in chats:
                await bot.send_message(cid, text, disable_web_page_preview=True)
                if total:
                    await bot.send_document(
                        cid, FSInputFile(str(path)),
                        caption=f"горит {len(hot)} · в файле {len(rest)}")
        finally:
            await bot.session.close()

    asyncio.run(send())
    log.info("Отправлено: горит %s, в файле %s", len(hot), len(rest))


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    banner()

    p = argparse.ArgumentParser(description="Дайджест пополнения склада")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dry-run", help="посчитать и напечатать")
    d.add_argument("--csv", default="digest.csv")
    d.add_argument("--debug", action="store_true")
    d.set_defaults(func=cmd_dry)

    o = sub.add_parser("once", help="отправить в Telegram и выйти")
    o.set_defaults(func=cmd_once)

    s = sub.add_parser("sources", help="что вернул каждый источник")
    s.set_defaults(func=cmd_sources)

    args = p.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:                                       # noqa: BLE001
        # Понятное сообщение вместо трейсбека: запускать будет не программист.
        # Полный стек остаётся в логе при --debug или в CI-логах.
        from ms import MoySkladError
        from wb import WBError
        if isinstance(e, (MoySkladError, WBError)):
            print(f"\nОшибка источника: {e}", file=sys.stderr)
            sys.exit(2)
        raise


if __name__ == "__main__":
    main()
