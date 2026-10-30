"""
Вкусняшки в упаковке (docs/13 §4). С этикетки человек вводит числа, всё
остальное считает код: граммы штуки, углеводы в ней, скорость подъёма сахара.
"""

from __future__ import annotations

from decimal import Decimal

TENTH = Decimal("0.1")

# Жир тормозит всасывание: по нему и делим. Это типичная картина, а не
# расчёт — в углеводы не входит, и человек может поставить своё по датчику.
FAST_FAT_BELOW = Decimal("5")
SLOW_FAT_FROM = Decimal("20")


def speed_by_fat(fat_100g: Decimal | None) -> str | None:
    if fat_100g is None:
        return None
    if fat_100g < FAST_FAT_BELOW:
        return "fast"
    if fat_100g < SLOW_FAT_FROM:
        return "mid"
    return "slow"


def carbs_100g(label: Decimal, fiber: Decimal | None, subtract: bool) -> Decimal:
    """Усвояемые углеводы, как в справочнике: клетчатку вычитаем, если попросили."""
    if subtract and fiber:
        return max(Decimal(0), label - fiber)
    return label


def piece_grams(piece_g: Decimal | None, pack_net_g: Decimal | None, pack_pieces: int | None) -> Decimal | None:
    """Взвешенная штука точнее среднего по пачке; нет ни того, ни другого — счёт только весом."""
    if piece_g:
        return piece_g
    if pack_net_g and pack_pieces:
        return (pack_net_g / pack_pieces).quantize(TENTH)
    return None
