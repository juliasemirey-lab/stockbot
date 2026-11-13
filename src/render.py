"""Текст дайджеста и CSV."""
from __future__ import annotations

import csv
import io

from calc import Item


def _positions(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "позициям"
    if n % 10 == 1:
        return "позиции"
    return "позициям"


def digest(need: list[Item], top_n: int = 15) -> str:
    out = ["Время пополнять склад!", ""]

    if not need:
        out.append("Товары: заказывать нечего")
        return "\n".join(out)

    total = sum(i.need for i in need)
    out.append(f"Товары: {total:.0f} шт. по {len(need)} {_positions(len(need))}")
    out.append("")

    for it in need[:top_n]:
        head = it.name
        if it.color:
            head += f" · {it.color}"
        if it.size:
            head += f" · {it.size}"

        line = f"Доступно {it.available:.0f}"
        if it.cover_days is not None:
            line += f" · хватит на {it.cover_days:.0f} дн."
        line += f" · заказать {it.need:.0f}"
        if it.wb_url:
            line += f"  [WB]({it.wb_url})"

        out.append(head)
        out.append(line)
        out.append("")

    if len(need) > top_n:
        out.append(f"_…и ещё {len(need) - top_n} — в CSV_")

    return "\n".join(out).rstrip()


def csv_bytes(need: list[Item]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Артикул", "Товар", "Цвет", "Размер",
                "Доступно", "Заказать", "Профиль", "WB"])
    for it in need:
        w.writerow([it.article, it.name, it.color, it.size,
                    f"{it.available:.0f}", f"{it.need:.0f}",
                    it.profile, it.wb_url])
    return "\ufeff".encode("utf-8") + buf.getvalue().encode("utf-8")
