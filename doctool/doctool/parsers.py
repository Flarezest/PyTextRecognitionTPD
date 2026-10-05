"""Разбор значений полей: даты (в т.ч. прописью), паспорт, ИНН, домены, контакты."""
from __future__ import annotations

import re
from datetime import date

DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s?[.,\-/]\s?(\d{1,2})\s?[.,\-/]\s?(\d{4}|\d{2})(?!\d)")
PASSPORT_RE = re.compile(r"(?<![\d+])(\d{2})\s?(\d{2})\s?[№N]?\s?(\d{6})(?!\d)")
INN_RE = re.compile(r"(?<![\d+])(\d{12}|\d{10})(?!\d)")
EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+", re.U)
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
DEPT_RE = re.compile(r"(?<!\d)(\d{3})\s?[-—–]\s?(\d{3})(?!\d)")
DOMAIN_RE = re.compile(
    r"(?<![@\w.\-])((?:[a-zа-яё0-9](?:[a-zа-яё0-9\-]{0,61}[a-zа-яё0-9])?\.)+"
    r"(?:рф|рус|дети|москва|онлайн|сайт|орг|su|ru|com|net|org|info|biz|pro|me|io|xn--p1ai|[a-z]{2,10}))(?![\w\-@])",
    re.I)


def parse_date(s: str, allow_short_year: bool = False) -> date | None:
    """ДД.ММ.ГГГГ. Двузначный год по умолчанию не принимается: «28.06.19» на бланке
    чаще всего обрезанное «19..», а не 2019 год."""
    for m in DATE_RE.finditer(s or ""):
        if len(m.group(3)) == 4 or allow_short_year:
            break
    else:
        return None
    d, mth, y = int(m.group(1)), int(m.group(2)), m.group(3)
    year = int(y) if len(y) == 4 else (2000 + int(y) if int(y) <= date.today().year % 100 else 1900 + int(y))
    try:
        return date(year, mth, d)
    except ValueError:
        return None


def fmt(d: date | None) -> str | None:
    return d.strftime("%d.%m.%Y") if d else None


# ----------------------------------------------------------- дата прописью

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
# порядок важен: длинные основы раньше коротких
NUM_STEMS = [
    ("одиннадцат", 11), ("двенадцат", 12), ("тринадцат", 13), ("четырнадцат", 14), ("пятнадцат", 15),
    ("шестнадцат", 16), ("семнадцат", 17), ("восемнадцат", 18), ("девятнадцат", 19),
    ("двадцат", 20), ("тридцат", 30), ("сорок", 40), ("пятидесят", 50), ("пятьдесят", 50),
    ("девяност", 90), ("десят", 10),
    ("перв", 1), ("одн", 1), ("один", 1), ("втор", 2), ("два", 2), ("две", 2), ("двух", 2),
    ("трет", 3), ("три", 3), ("четвер", 4), ("четыр", 4), ("пят", 5), ("шест", 6),
    ("седьм", 7), ("сем", 7), ("восьм", 8), ("восем", 8), ("девят", 9),
]


def _word_num(w: str) -> int | None:
    w = w.lower().replace("ё", "е")
    if w.startswith("тысяч"):
        return 1000
    for stem, v in NUM_STEMS:
        if w.startswith(stem):
            return v
    return None


def parse_date_words(s: str) -> date | None:
    """«Седьмое августа две тысячи двадцать шестого года», «"25" сентября 2026 г.»"""
    if not s:
        return None
    if (d := parse_date(s)):
        return d
    text = s.lower().replace("ё", "е")
    mm = None
    for stem, v in MONTHS.items():
        m = re.search(rf"\b{stem}[а-я]*\b", text)
        if m and (stem != "ма" or re.search(r"\bмая\b", text)):
            mm, pos = v, m
            break
    if not mm:
        # рукопись: месяц прочитан с ошибками («сентябре», «сешпября») — нечёткое сравнение
        from rapidfuzz import fuzz
        full = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                "сентября", "октября", "ноября", "декабря"]
        best = (0, None, None)
        for m in re.finditer(r"[а-я]{4,}", text):
            for i, name in enumerate(full):
                sc = fuzz.ratio(m.group(0), name)
                if sc > best[0]:
                    best = (sc, i + 1, m)
        if best[0] >= 70:
            _, mm, pos = best
    if not mm:
        return None
    before, after = text[:pos.start()], text[pos.end():]
    # день: цифры или слова перед месяцем
    day = None
    if (m := re.search(r"(\d{1,2})\D*$", before)):
        day = int(m.group(1))
    else:
        nums = [_word_num(w) for w in re.findall(r"[а-я]+", before)]
        nums = [n for n in nums if n]
        if nums:
            day = sum(nums[-2:]) if len(nums) >= 2 and nums[-2] >= 20 and nums[-1] < 10 else nums[-1]
    # год: цифры или слова после месяца
    year = None
    if (m := re.search(r"(\d{4})", after)):
        year = int(m.group(1))
    elif (m := re.search(r"20\s*(\d{2})", after)):
        year = 2000 + int(m.group(1))
    else:
        total, cur = 0, 0
        for w in re.findall(r"[а-я]+", after):
            n = _word_num(w)
            if n is None:
                continue
            if n == 1000:
                total += (cur or 1) * 1000
                cur = 0
            else:
                cur += n
        year = total + cur if total else None
    try:
        return date(year, mm, day) if (year and day) else None
    except ValueError:
        return None


# ----------------------------------------------------------- проверки форматов

def inn_valid(inn: str) -> bool:
    """Контрольные разряды ИНН (10 цифр — юрлицо, 12 — физлицо/ИП)."""
    if not re.fullmatch(r"\d{10}|\d{12}", inn or ""):
        return False
    d = list(map(int, inn))

    def cs(coefs, digits):
        return sum(c * x for c, x in zip(coefs, digits)) % 11 % 10

    if len(d) == 10:
        return cs([2, 4, 10, 3, 5, 9, 4, 6, 8], d) == d[9]
    return (cs([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d) == d[10]
            and cs([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d) == d[11])


def normalize_passport(s: str) -> str | None:
    m = PASSPORT_RE.search(s or "")
    return f"{m.group(1)}{m.group(2)} {m.group(3)}" if m else None


TLD_HAND = r"(ru|рф|su|com|net|org|рус|online|site|pro|info)"


def find_domains(s: str) -> list[str]:
    """Домены из текста. Для рукописи: точку перед зоной часто пишут как «-», «_», «,» или пробел
    («distobR-RU»), а кириллические буквы путают с латинскими."""
    s = s or ""
    if not DOMAIN_RE.search(s):
        s = re.sub(rf"(?<=[A-Za-zА-Яа-яЁё0-9])\s*[-_,·.]\s*{TLD_HAND}\b", r".\1", s, flags=re.I)
        s = re.sub(rf"(?<=[A-Za-zА-Яа-яЁё0-9])\s+{TLD_HAND}\s*$", r".\1", s.strip(), flags=re.I)
    out = []
    for m in DOMAIN_RE.finditer(s):
        d = m.group(1).lower().rstrip(".")
        if d not in out and not re.fullmatch(r"[\d.]+", d):
            out.append(d)
    return out


def to_punycode(domain: str) -> str:
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        return domain
