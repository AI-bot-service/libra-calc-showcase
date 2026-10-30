"""
Цифры штрихкода без базы: контрольная, нормализация, внутренние коды сети.

Коды ограниченного обращения (префикс 2) сеть раздаёт сама: у другой сети
тот же код — другой товар. Бывают двух видов:
- штучный товар своей марки или фасовки: «Монетка» 2900001102810, масса
  напечатана на пачке, в коде её нет;
- весовой: 2X PPPPP WWWWW C — пять цифр перед контрольной у большинства сетей
  РФ масса в граммах, у части — цена.
Весовой узнаётся только сверкой с массой на наклейке: совпала — в ключ товара
идёт код с нулями вместо массы, иначе код целиком.
"""

from __future__ import annotations

import re
from decimal import Decimal

# Сети, которые человек может выбрать сам, когда сеть на фото не видна: только
# те, чьи каталоги у нас есть. Модель с этикетки читает любую — она идёт в ключ как есть
OUR_SHOPS = {"metro": "Metro", "auchan": "Ашан"}
_ALIASES = {"метро": "metro", "metro cash carry": "metro", "ашан": "auchan", "auchan": "auchan"}
# Совпадение массы из кода с наклейкой, граммы: весы округляют до грамма
WEIGHT_MATCH_G = 2


def check_digit(body: str) -> str:
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def normalize(code: str) -> str | None:
    """EAN-13 или EAN-8 цифрами; UPC-A и GTIN-14 с ведущим нулём — как EAN-13. Неверная контрольная — None."""
    code = code.strip()
    if not re.fullmatch(r"\d{8}|\d{12,14}", code) or check_digit(code[:-1]) != code[-1]:
        return None
    if len(code) == 12:
        code = "0" + code
    if len(code) == 14 and code[0] == "0":
        code = code[1:]
    if len(code) == 13 and code.startswith("00000"):
        code = code[5:]
    return code


def restricted(code: str) -> bool:
    return len(code) in (8, 13) and code[0] == "2"


def weight_guess(code: str) -> int | None:
    """Масса из весового кода, граммы, если формат 2X PPPPP WWWWW C. Подсказка, а не число в форму."""
    if len(code) != 13 or code[0] != "2":
        return None
    grams = int(code[7:12])
    return grams if 1 <= grams <= 30000 else None


def _zero_weight(code: str) -> str:
    body = code[:7] + "00000"
    return body + check_digit(body)


def item_code(code: str, pack_net_g: Decimal | float | None) -> str:
    """Код товара для общей базы: у весового — без массы, чтобы 412 г и 380 г были одним товаром."""
    grams = weight_guess(code)
    if grams is not None and pack_net_g is not None and abs(grams - float(pack_net_g)) <= WEIGHT_MATCH_G:
        return _zero_weight(code)
    return code


def lookup_codes(code: str) -> list[str]:
    """Под какими кодами товар может лежать в общей базе: целиком и, если код похож на весовой, без массы."""
    if weight_guess(code) is None:
        return [code]
    return list(dict.fromkeys([code, _zero_weight(code)]))


def shop_key(raw: str | None) -> str | None:
    """«ТС «Монетка»», «METRO Cash & Carry» → монетка, metro. Пусто — None."""
    if not raw:
        return None
    s = raw.lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\b(тс|ооо|ао|зао|сеть|магазин|магазинов|гипермаркет|супермаркет)\b", " ", s)
    s = " ".join(s.split())[:40]
    if not s:
        return None
    return _ALIASES.get(s, next((v for k, v in _ALIASES.items() if s.startswith(k)), s))
