"""Тестовый индекс баркодов WB: nmID + skus, как в content/v2/get/cards/list.

Строится из той же демо-выгрузки, но часть баркодов намеренно отсутствует —
чтобы проверить и совпадения, и поиск-заглушку, и позиции без баркода.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wb_client import normalize_barcode  # noqa: E402


def build(dump_path: str, out_path: str, missing_every: int = 7) -> None:
    rows = json.loads(Path(dump_path).read_text(encoding="utf-8"))
    index: dict[str, dict] = {}
    nmid = 15_000_000

    for i, r in enumerate(rows):
        for bc in r.get("barcodes") or []:
            if i % missing_every == 0:
                continue          # имитируем «карточки нет на WB»
            nmid += 1
            index[normalize_barcode(bc)] = {
                "nmid": nmid,
                "url": f"https://www.wildberries.ru/catalog/{nmid}/detail.aspx",
                "vendor_code": r.get("article", ""),
                "size_name": r.get("size", ""),
            }

    Path(out_path).write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    print(f"WB-индекс: {len(index)} баркодов -> {out_path}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dump.json",
          sys.argv[2] if len(sys.argv) > 2 else "/tmp/wb_barcode_index.json")
