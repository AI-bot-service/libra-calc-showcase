"""
Внутренние коды сети (префикс 2): штучный товар своей марки и весовой.
Весовой узнаётся только сверкой массы из кода с наклейкой.
"""

from decimal import Decimal

from libra.ean import item_code, lookup_codes, shop_key

# Манго сушёное «Монетки», 80 г: в разрядах массы 10281 — это не масса
MANGO = "2900001102810"
# Весовое печенье: 2X PPPPP 00412 C — 412 г
COOKIES_412 = "2112345004129"


def test_штучный_код_на_2_целиком():
    assert item_code(MANGO, Decimal("80")) == MANGO
    assert item_code(MANGO, None) == MANGO


def test_весовой_без_массы_один_товар():
    zero = item_code(COOKIES_412, Decimal("412"))
    assert zero.startswith("2112345") and zero[7:12] == "00000"
    assert zero in lookup_codes(COOKIES_412)


def test_сеть_с_этикетки_в_ключ():
    assert shop_key("ТС «Монетка»") == "монетка"
    assert shop_key("Ашан") == shop_key("AUCHAN") == "auchan"
    assert shop_key("  ") is None
