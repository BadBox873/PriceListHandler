from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from functools import cached_property


class UserError(Exception):
    """Expected, actionable error suitable for the Russian UI."""


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().replace("ё", "е").split())


def decimal_value(value: str | None) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise UserError(f"Некорректное число: {value}") from exc
    if not number.is_finite():
        raise UserError("Число должно быть конечным.")
    return number


def quantity(value: str | int) -> int:
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,12}", text) or int(text) <= 0:
        raise UserError("Количество — целое число от 1 до 999999999999. Для удаления используй отдельную кнопку.")
    return int(text)


@dataclass(frozen=True)
class Product:
    row: int
    code: str
    name: str
    detail: str
    barcode: str
    price: Decimal | None
    stock: Decimal | None
    original: str | None = None
    original_type: str = "n"

    @cached_property
    def search_text(self) -> str:
        return normalize(f"{self.name} {self.detail} {self.code} {self.barcode}")

    def error(self, value: str | int, *, imported: bool = False) -> str:
        try:
            if imported:
                n = decimal_value(str(value))
                if n is None or n <= 0 or n != n.to_integral_value():
                    return "Количество должно быть положительным целым"
                if n > 999999999999:
                    return "Слишком большое количество"
            else:
                n = Decimal(quantity(value))
        except UserError as exc:
            return str(exc)
        if self.stock is None:
            return "Остаток неизвестен — заказ запрещён"
        if n > self.stock:
            return f"Недостаточный остаток: доступно {self.stock}, требуется {n}"
        return ""


def search(products: list[Product], query: str, *, available: bool = False,
           priced: bool = False, ordered: set[str] | None = None,
           fuzzy: bool = True) -> tuple[list[Product], bool]:
    words = normalize(query).split()
    candidates = [p for p in products if (not available or (p.stock is not None and p.stock >= 1))
                  and (not priced or p.price is not None) and (ordered is None or p.code in ordered)]
    direct = [p for p in candidates if all(w in p.search_text for w in words)]
    if direct or not words or not fuzzy:
        return direct, False
    needle = normalize(query)
    ranked = []
    for p in candidates:
        name = normalize(p.name)
        word_scores = [max((SequenceMatcher(None, w, t).ratio() for t in name.split()), default=0) for w in words]
        score = max(SequenceMatcher(None, needle, name).ratio(), sum(word_scores) / len(words))
        if score >= .66:
            ranked.append((score, p.row, p))
    ranked.sort(key=lambda v: (-v[0], v[1]))
    return [v[2] for v in ranked[:40]], True


def totals(products: dict[str, Product], items: dict[str, str]) -> tuple[Decimal, bool]:
    total, incomplete = Decimal(0), False
    for code, value in items.items():
        product = products[code]
        try:
            n = decimal_value(value)
            if n is None or n <= 0 or n != n.to_integral_value():
                incomplete = True
                continue
        except UserError:
            incomplete = True
            continue
        if product.price is None:
            incomplete = True
        else:
            total += product.price * n
    return total, incomplete


def money(value: Decimal | None) -> str:
    return "Цена не указана" if value is None else f"{value:,.2f}".replace(",", " ") + " ₽"
