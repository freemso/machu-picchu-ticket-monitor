from __future__ import annotations

import re
import unicodedata

ROUTE_CODE_RE = re.compile(
    r"(?:circuito|circuit|ruta|route)?\s*([1-3])\s*[-\s]?\s*([a-d])\b",
    re.IGNORECASE,
)


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_route_code(value: str) -> str:
    cleaned = strip_accents(value).strip().upper()
    match = ROUTE_CODE_RE.search(cleaned)
    if match:
        return f"{match.group(1)}{match.group(2).upper()}"

    compact = re.sub(r"[^A-Z0-9]", "", cleaned)
    if re.fullmatch(r"[1-3][A-D]", compact):
        return compact
    return cleaned


def route_code_from_text(value: str) -> str | None:
    match = ROUTE_CODE_RE.search(strip_accents(value))
    if not match:
        return None
    return f"{match.group(1)}{match.group(2).upper()}"


def display_route(value: str) -> str:
    code = normalize_route_code(value)
    return f"Circuit {code[0]}{code[1]}" if re.fullmatch(r"[1-3][A-D]", code) else value
