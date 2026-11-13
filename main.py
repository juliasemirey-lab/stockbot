"""Точка входа: dry-run, once (для GitHub Actions), order (локально), run (сервер).

Три режима соответствуют трём сценариям:
  once   — короткий job по расписанию, отправил и вышел (GitHub Actions)
  order  — создать черновик заказа поставщику, без Telegram
  run    — долгоживущий бот с командами (нужен сервер 24/7)
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

from calc import build_lines, group_by_supplier, pair_separates          # noqa: E402
from pipeline import build_rows_from_dump, build_rows_from_ms           # noqa: E402
from render import render_csv, render_summary, render_suppliers         # noqa: E402

log = logging.getLogger("stockbot")


def _deep_merge(base: dict, over: dict) -> dict:
    """config.local.yaml перекрывает config.yaml, не затирая блоки целиком."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # локальные переопределения: реальные поставщики, контакты, сроки.
    # Файл в .gitignore — в публичный репозиторий не попадает.
    local = Path(path).with_name("config.local.yaml")
    if local.exists():
        with open(local, encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})
        log.info("Применены локальные настройки: %s", local)
    return cfg


def collect(cfg: dict, with_links: bool = True):
    today = date.today()
    dump = cfg.get("_dump") or os.environ.get("STOCKBOT_DUMP")
    if dump:
        rows = build_rows_from_dump(dump, cfg, with_links=with_links)
    else:
        from ms_client import MS
        rows = build_rows_from_ms(MS(), cfg, with_links=with_links)
    lines = build_lines(rows, cfg, today)
    return lines, today, pair_separates(lines)


def _chat_ids() -> list[int]:
    raw = os.environ.get("TELEGRAM_CHAT_IDS") or ""
    return [int(x) for x in raw.split(",") if x.strip()]


def cmd_dry(args):
    cfg = load_cfg(args.config)
    if args.dump:
        cfg["_dump"] = args.dump
    lines, today, warns = collect(cfg, with_links=args.wb)
    parts = [render_summary(lines, today, warns, top_n=int(cfg.get("top_n_in_chat", 8)))]
    parts += render_suppliers(lines, cfg)
    print(("\n\n" + "─" * 40 + "\n\n").join(parts))
    if args.csv:
        Path(args.csv).write_bytes(render_csv(lines, cfg, only_action=not args.csv_all))
        n = len(lines) if args.csv_all else len([x for x in lines if x.need > 0])
        print(f"\nCSV: {args.csv} ({n} строк)", file=sys.stderr)


def cmd_once(args):
    """Один запуск: посчитать, отправить в Telegram, выйти.

    Для GitHub Actions и системного cron — там нет долгоживущего процесса.
    """
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import FSInputFile

    cfg = load_cfg(args.config)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Нужен TELEGRAM_BOT_TOKEN в переменных окружения.")
    chat_ids = _chat_ids()
    if not chat_ids:
        sys.exit("Нужен TELEGRAM_CHAT_IDS (через запятую).")

    lines, today, warns = collect(cfg)
    action = [ln for ln in lines if ln.need > 0]
    summary = render_summary(lines, today, warns, top_n=int(cfg.get("top_n_in_chat", 8)))
    supplier_msgs = render_suppliers(lines, cfg)

    csv_path = Path(f"digest_{today:%Y%m%d}.csv")
    csv_path.write_bytes(render_csv(lines, cfg, only_action=True))

    async def send():
        bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
        try:
            for cid in chat_ids:
                await bot.send_message(cid, summary, disable_web_page_preview=True)
                for msg in supplier_msgs:
                    await bot.send_message(cid, msg, disable_web_page_preview=True)
                await bot.send_document(
                    cid, FSInputFile(str(csv_path)),
                    caption=f"Закупка: {len(action)} позиций из {len(lines)} просмотренных",
                )
        finally:
            await bot.session.close()

    asyncio.run(send())
    log.info("Отправлено: %s позиций к заказу, чатов %s", len(action), len(chat_ids))


def cmd_order(args):
    """Черновик заказа поставщику без Telegram — локальное действие.

    Ссылки на карточки WB здесь не нужны, поэтому выгрузка карточек
    отключается: заказ создаётся в разы быстрее.
    """
    import orders

    cfg = load_cfg(args.config)
    if args.dump:
        cfg["_dump"] = args.dump
    cfg.setdefault("wildberries", {})["enabled"] = False

    lines, today, _ = collect(cfg, with_links=False)
    groups = group_by_supplier(lines)
    if not groups:
        print("Нечего заказывать: нет позиций с потребностью.")
        return

    needle = (args.supplier or "all").strip().lower()
    if needle != "all":
        groups = {k: v for k, v in groups.items() if needle in k.lower()}
        if not groups:
            print(f"Нет позиций к заказу у «{args.supplier}». Доступные:")
            for name in sorted(group_by_supplier(lines)):
                print("  ·", name)
            sys.exit(1)

    if args.dry:
        for supplier, items in groups.items():
            print(f"\n{supplier}: {len(items)} поз., {sum(x.need for x in items):.0f} шт")
            for ln in items:
                print(f"  {ln.article:20} {ln.name} {ln.color} {ln.size}  +{ln.need:.0f}")
        print("\n(dry-run: документы не создавались)")
        return

    for supplier, items in groups.items():
        total = sum(x.need for x in items)
        try:
            name, n, link = orders.create_order(items, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"✗ {supplier}: заказ не создан — {e}")
            continue
        print(f"✓ {supplier}: черновик «{name}», {n} поз., {total:.0f} шт")
        print(f"  {link}")

    print("\nДокументы не проведены — проверьте и проведите в МойСклад.")


def cmd_run(args):
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    import bot as botmod

    cfg = load_cfg(args.config)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Нужен TELEGRAM_BOT_TOKEN в переменных окружения.")

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    botmod.CFG = cfg

    sched = AsyncIOScheduler(timezone=cfg.get("timezone", "Europe/Moscow"))
    sc = cfg.get("schedule", {})
    sched.add_job(
        botmod.push_digest,
        CronTrigger(day_of_week=sc.get("day_of_week", "wed"),
                    hour=int(sc.get("hour", 10)),
                    minute=int(sc.get("minute", 0))),
        kwargs={"bot": bot, "chat_ids": _chat_ids(), "cfg": cfg},
        id="weekly_digest", replace_existing=True,
    )
    sched.start()
    log.info("Планировщик: %s %02d:%02d, склад: %s",
             sc.get("day_of_week", "wed"), int(sc.get("hour", 10)),
             int(sc.get("minute", 0)), cfg.get("warehouse"))

    try:
        asyncio.run(botmod.dp.start_polling(bot))
    finally:
        sched.shutdown()
        asyncio.run(bot.session.close())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Дайджест остатков и закупки")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dry-run", help="посчитать и напечатать без Telegram")
    d.add_argument("--dump", help="JSON-выгрузка вместо API")
    d.add_argument("--csv", default="digest.csv")
    d.add_argument("--csv-all", action="store_true",
                   help="выгрузить все SKU, включая без движения и с достатком")
    d.add_argument("--wb", action="store_true", help="искать ссылки на карточки WB")
    d.set_defaults(func=cmd_dry)

    o = sub.add_parser("once", help="один запуск: отправить дайджест и выйти")
    o.set_defaults(func=cmd_once)

    c = sub.add_parser("order", help="создать черновик заказа поставщику")
    c.add_argument("supplier", nargs="?", default="all",
                   help="часть имени поставщика или all")
    c.add_argument("--dry", action="store_true", help="показать, но не создавать")
    c.add_argument("--dump", help="JSON-выгрузка вместо API")
    c.set_defaults(func=cmd_order)

    r = sub.add_parser("run", help="бот + планировщик (нужен сервер 24/7)")
    r.set_defaults(func=cmd_run)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
