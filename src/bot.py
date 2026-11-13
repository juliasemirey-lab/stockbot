"""Telegram-бот: тонкий слой. Логика — в calc/pipeline/render."""
from __future__ import annotations

import logging
from datetime import date

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from calc import build_lines, dow_weights, month_factor, pair_separates
from ms_client import MS
from pipeline import build_rows_from_dump, build_rows_from_ms
from render import explain, render_csv, render_summary, render_suppliers

log = logging.getLogger("stockbot")
dp = Dispatcher()

CFG: dict = {}
CACHE: dict = {"lines": [], "today": None, "warns": []}


def collect(cfg: dict):
    today = date.today()
    dump = cfg.get("_dump")
    rows = build_rows_from_dump(dump, cfg) if dump else build_rows_from_ms(MS(), cfg)
    lines = build_lines(rows, cfg, today)
    warns = pair_separates(lines)
    CACHE.update(lines=lines, today=today, warns=warns)
    return lines, today, warns


async def send_digest(target, bot: Bot | None = None):
    """Сводка + сообщение на каждого поставщика + CSV."""
    lines, today, warns = collect(CFG)
    summary = render_summary(lines, today, warns, cfg=CFG, top_n=int(CFG.get("top_n_in_chat", 10)))
    path = f"/tmp/digest_{today:%Y%m%d}.csv"
    with open(path, "wb") as f:
        f.write(render_csv(lines, CFG, only_action=True))
    action = [ln for ln in lines if ln.need > 0]

    async def say(text):
        if isinstance(target, Message):
            await target.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await bot.send_message(target, text, parse_mode="Markdown",
                                   disable_web_page_preview=True)

    await say(summary)
    for msg in render_suppliers(lines, CFG):
        await say(msg)

    footer = (f"📄 Закупка: {len(action)} позиций из {len(lines)} просмотренных")
    fname = f"digest_{today:%Y%m%d}.csv"
    if isinstance(target, Message):
        await target.answer(footer)
        await target.answer_document(FSInputFile(path, filename=fname))
    else:
        await bot.send_document(target, FSInputFile(path, filename=fname), caption=footer)


@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(
        "Дайджест по доступному остатку и закупке.\n\n"
        "/digest — сводка + разбивка по поставщикам + CSV\n"
        "/supplier <имя> — только один поставщик\n"
        "/order <имя> — черновик заказа в МойСклад\n"
        "/why <артикул> — разбор расчёта и сопоставление с WB\n"
        "/reload — пересчитать\n\n"
        "Автоматически приходит каждый понедельник в 08:00."
    )


@dp.message(Command("digest"))
async def digest_now(msg: Message):
    await msg.answer("Считаю…")
    await send_digest(msg)


@dp.message(Command("supplier"))
async def supplier_only(msg: Message):
    arg = (msg.text or "").split(maxsplit=1)
    if len(arg) < 2:
        await msg.answer("Использование: `/supplier Текстиль`")
        return
    needle = arg[1].strip().lower()
    lines = CACHE["lines"] or collect(CFG)[0]
    subset = [ln for ln in lines if needle in (ln.supplier or "").lower()]
    if not subset:
        await msg.answer("Поставщик не найден. /digest покажет список.")
        return
    name = subset[0].supplier
    cfg = dict(CFG)
    cfg["suppliers"] = {name: (CFG.get("suppliers") or {}).get(name, {})}
    for m in render_suppliers(subset, cfg):
        await msg.answer(m, parse_mode="Markdown", disable_web_page_preview=True)


@dp.message(Command("order"))
async def order_cmd(msg: Message):
    import orders
    from calc import group_by_supplier

    arg = (msg.text or "").split(maxsplit=1)
    if len(arg) < 2:
        await msg.answer("Использование: `/order <поставщик>` или `/order all`")
        return

    lines = CACHE["lines"] or collect(CFG)[0]
    groups = group_by_supplier(lines)
    needle = arg[1].strip().lower()
    if needle != "all":
        groups = {k: v for k, v in groups.items() if needle in k.lower()}
        if not groups:
            await msg.answer("Нет позиций к заказу у этого поставщика.")
            return

    for supplier, items in groups.items():
        total = sum(x.need for x in items)
        try:
            name, n, link = orders.create_order(items, CFG)
        except Exception as e:  # noqa: BLE001
            await msg.answer(f"*{supplier}*: заказ не создан — {e}", parse_mode="Markdown")
            continue
        await msg.answer(
            f"✅ *{supplier}*: черновик заказа *{name}*\n"
            f"{n} позиций, {total:.0f} шт\n"
            f"Документ не проведён — проверьте и проведите:\n{link}",
            parse_mode="Markdown", disable_web_page_preview=True,
        )


@dp.message(Command("why"))
async def why(msg: Message):
    arg = (msg.text or "").split(maxsplit=1)
    if len(arg) < 2:
        await msg.answer("Использование: `/why WO-OSLO-M`")
        return
    needle = arg[1].strip().lower()
    lines = CACHE["lines"] or collect(CFG)[0]
    hits = [ln for ln in lines
            if needle in (ln.article or "").lower()
            or needle in ln.sku_id.lower()
            or needle in (ln.barcode or "")]
    if not hits:
        await msg.answer("Не нашёл. Запустите /digest для пересчёта.")
        return
    ln = hits[0]
    seas = (CFG.get("seasonality") or {}).get(ln.category) or [1.0] * 12
    note = (f"коэффициент месяца {month_factor(seas, date.today()):.2f} "
            f"(нормированный, средний по году = 1.00)")
    dow = dow_weights(CFG.get("dow_profile") or [1.0] * 7, date.today(),
                      int(CFG.get("horizon_days", 7)))
    await msg.answer(explain(ln, dow, note), parse_mode="Markdown",
                     disable_web_page_preview=True)


@dp.message(Command("reload"))
async def reload_cmd(msg: Message):
    lines, today, _ = collect(CFG)
    await msg.answer(f"Пересчитано: {len(lines)} позиций на {today:%d.%m.%Y}.")


@dp.message(F.text & ~F.text.startswith("/"))
async def fallback(msg: Message):
    await msg.answer("Команды: /digest, /supplier <имя>, /order <имя>, /why <артикул>")


async def push_digest(bot: Bot, chat_ids: list[int], cfg: dict):
    """Задача планировщика: понедельник, 08:00."""
    for cid in chat_ids:
        try:
            await send_digest(cid, bot)
        except Exception as e:  # noqa: BLE001
            log.warning("Не доставлено в %s: %s", cid, e)
