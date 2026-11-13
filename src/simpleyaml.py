"""Минимальный разборщик YAML — замена PyYAML для config.yaml.

Зачем: на самых новых версиях Python (3.15+) готового пакета PyYAML ещё
нет, и pip пытается собрать его из исходников, требуя компилятор C++.
А конфиг — это простой набор вложенных словарей, списков и строк.

Поддерживается ровно то, что используется в config.yaml:
  * key: value                 (вложенность по отступу)
  * key: [1, 2, 3]             (список в строке)
  * key: {a: 1, b: 2}          (словарь в строке)
  * - элемент                  (список с новой строки)
  * комментарии #, пустые строки
  * строки в кавычках, числа, true/false, null

Если PyYAML установлен — используется он (он надёжнее).
"""
from __future__ import annotations

import re

try:                                  # есть — берём его
    import yaml as _real_yaml
except ImportError:                   # нет — работаем своим
    _real_yaml = None


def parse(text: str) -> dict:
    """Разбирает YAML-текст в словарь."""
    if _real_yaml is not None:
        return _real_yaml.safe_load(text) or {}

    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # комментарий в конце строки, но не внутри кавычек
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        lines.append((len(line) - len(line.lstrip()), line.strip()))

    value, _ = _parse_block(lines, 0, 0)
    return value if isinstance(value, dict) else {}


def safe_load(stream) -> dict:
    """Совместимо с PyYAML: принимает строку или открытый файл."""
    if _real_yaml is not None:
        return _real_yaml.safe_load(stream) or {}
    if hasattr(stream, "read"):
        stream = stream.read()
    return parse(stream)


def _strip_comment(s: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def _parse_block(lines, idx, indent):
    """Разбирает блок на заданном отступе. Возвращает (значение, новый индекс)."""
    if idx >= len(lines):
        return {}, idx

    if lines[idx][1].startswith("- "):
        return _parse_list(lines, idx, indent)
    return _parse_map(lines, idx, indent)


def _parse_map(lines, idx, indent):
    result: dict = {}
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent < indent:
            break
        if cur_indent > indent:            # продолжение предыдущего ключа
            idx += 1
            continue
        if content.startswith("- "):
            break

        if ":" not in content:
            idx += 1
            continue

        key, _, rest = content.partition(":")
        key = _scalar(key.strip())
        rest = rest.strip()

        if rest:
            result[key] = _scalar(rest)
            idx += 1
        else:
            # значение ушло на следующие строки
            nxt = idx + 1
            if nxt < len(lines) and lines[nxt][0] > cur_indent:
                child_indent = lines[nxt][0]
                result[key], idx = _parse_block(lines, nxt, child_indent)
            elif nxt < len(lines) and lines[nxt][0] == cur_indent and lines[nxt][1].startswith("- "):
                result[key], idx = _parse_list(lines, nxt, cur_indent)
            else:
                result[key] = None
                idx += 1
    return result, idx


def _parse_list(lines, idx, indent):
    items: list = []
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent != indent or not content.startswith("- "):
            break
        body = content[2:].strip()
        if body and ":" in body and not body.startswith(("[" , "{", "\"", "'")):
            # словарь внутри списка: "- code: ozon"
            sub_indent = indent + 2
            sub, idx = _parse_map(
                [(sub_indent, body)] + lines[idx + 1:], 0, sub_indent
            )
            items.append(sub)
            # пересчитываем реальный индекс
            consumed = 1
            while (idx + consumed - 1) < len(lines) and lines[idx + consumed - 1][0] > indent:
                consumed += 1
            idx += consumed - 1
        else:
            items.append(_scalar(body))
            idx += 1
    return items, idx


def _scalar(s: str):
    """Превращает строку в нужный тип."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None

    # список в строке [a, b, c]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in _split_top(inner)]

    # словарь в строке {a: 1, b: 2}
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        out = {}
        if inner:
            for part in _split_top(inner):
                k, _, v = part.partition(":")
                out[_scalar(k.strip())] = _scalar(v.strip())
        return out

    # кавычки
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]

    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~", ""):
        return None

    # число
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)

    return s


def _split_top(s: str) -> list[str]:
    """Делит по запятым, не залезая внутрь скобок и кавычек."""
    parts, buf, depth, quote = [], [], 0, None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p != ""]
