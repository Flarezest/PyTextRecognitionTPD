"""Разбор значений полей: даты (в т.ч. прописью), паспорт, ИНН, домены, контакты."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# второй разделитель может быть прочитан OCR как «:» («20.01:2018»)
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s?[.,\-/]\s?(\d{1,2})\s?[.,\-/:]\s?(\d{4}|\d{2})(?!\d)")
# «27 апреля 1988 года», «10 апреля 2019 г.»
DATE_WORDS_RE = re.compile(r"(?<!\d)(\d{1,2})\s+(январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)"
                           r"[а-яё]*\.?\s+(\d{4})(?!\d)", re.I)
PASSPORT_RE = re.compile(r"(?<![\d+])(\d{2})\s?(\d{2})\s?[№N]?\s?(\d{6})(?!\d)")
INN_RE = re.compile(r"(?<![\d+])(\d{12}|\d{10})(?!\d)")
EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+", re.U)
PHONE_RE = re.compile(r"(?<![\d\w])(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
DEPT_RE = re.compile(r"(?<!\d)(\d{3})\s?[-—–]\s?(\d{3})(?!\d)")
DOMAIN_RE = re.compile(
    r"(?<![@\w.\-])((?:[a-zа-яё0-9](?:[a-zа-яё0-9\-]{0,61}[a-zа-яё0-9])?\.)+"
    r"(?:рф|рус|дети|москва|онлайн|сайт|орг|su|ru|com|net|org|info|biz|pro|me|io|xn--p1ai|[a-z]{2,10}))(?![\w\-@])",
    re.I)


def parse_date(s: str, allow_short_year: bool = False) -> date | None:
    """ДД.ММ.ГГГГ или «27 апреля 1988» (первая дата в строке). Двузначный год по умолчанию не принимается: «28.06.19» на бланке
    чаще всего обрезанное «19..», а не 2019 год."""
    m = next((m for m in DATE_RE.finditer(s or "") if len(m.group(3)) == 4 or allow_short_year), None)
    w = DATE_WORDS_RE.search(s or "")
    if w is not None and (m is None or w.start() < m.start()):
        mth = next(n for stem, n in MONTHS.items() if w.group(2).lower().startswith(stem))
        try:
            return date(int(w.group(3)), mth, int(w.group(1)))
        except ValueError:
            return None
    if m is None:
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
    """Домены из текста. Имена, разорванные переносом строки («business-⏎dvd.ru»), сначала склеиваются
    (см. join_wrapped_domains). Для рукописи: точку перед зоной часто пишут как «-», «_», «,» или пробел
    («distobR-RU»), а кириллические буквы путают с латинскими."""
    s = join_wrapped_domains(s or "")
    s = re.sub(r"(?<=[\w\-])\.pyc(?![\w\-])", ".рус", s, flags=re.I)   # «.рус», прочитанное латиницей
    if not DOMAIN_RE.search(s):
        s = re.sub(rf"(?<=[A-Za-zА-Яа-яЁё0-9])\s*[-_,·.]\s*{TLD_HAND}\b", r".\1", s, flags=re.I)
        s = re.sub(rf"(?<=[A-Za-zА-Яа-яЁё0-9])\s+{TLD_HAND}\s*$", r".\1", s.strip(), flags=re.I)
    out = []
    for m in DOMAIN_RE.finditer(s):
        d = m.group(1).lower().rstrip(".")
        if d.startswith("www.") and d.count(".") >= 2:
            d = d[4:]                       # «www.dr-sergeev.ru» → «dr-sergeev.ru»
        if d not in out and not re.fullmatch(r"[\d.]+", d):
            out.append(d)
    return out


def to_punycode(domain: str) -> str:
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        return domain


# ----------------------------------------------------------- список доменов: переносы строк, обрывки, «всего N»
#
# Где кончается имя домена. Имя состоит из меток [буквы, цифры, дефис], разделённых точками, и кончается зоной
# (.ru, .рф …). Метка не может начинаться или кончаться дефисом. Отсюда правила для длинных списков,
# где имя переносится на следующую строку:
#   «business-⏎dvd.ru»  — строка кончается дефисом: имя не закончено, продолжение — на следующей строке.
#                         Дефис остаётся: Word переносит строку после дефиса, который входит в имя;
#   «example.⏎ru», «example⏎.ru» — разрыв у точки перед зоной;
#   «info-dvd.ru-⏎…»     — слева уже полное имя с зоной: это не перенос, ничего не склеиваем.
# Перенос без дефиса посреди метки («cybersant⏎investor.ru») по тексту не отличить от двух слов — такой кусок
# без зоны попадает в «обрывки», и поле уходит на ручную проверку.

# зоны, на которых имя заканчивается (склейка «example.⏎ru» и проверка «слева уже полное имя»)
WRAP_TLDS = ("xn--p1ai", "online", "moscow", "store", "info", "shop", "site", "club", "рус", "biz", "com", "net",
             "org", "pro", "spb", "msk", "рф", "ru", "su", "me", "io")
_TLD_ALT = "|".join(WRAP_TLDS)
_LBL = r"[a-zа-яё0-9]"
_TCH = r"[a-zа-яё0-9.\-]"
_HYPHENS_NL = "-\u2010\u2011\u2012–—\u00ad"   # дефис, его юникод-варианты, тире (OCR), мягкий перенос
_HYPHENS_SP = "-\u2010\u2011\u00ad"
# дефис в конце строки: «business-⏎dvd.ru»
_WRAP_HYPHEN_NL = re.compile(rf"(?<![\w.\-@])({_LBL}{_TCH}*?)([{_HYPHENS_NL}])[ \t]*\r?\n\s*({_LBL}{_TCH}*)", re.I)
# то же, если переводы строк уже заменены пробелами: «business- dvd.ru» (метка не кончается дефисом)
_WRAP_HYPHEN_SP = re.compile(rf"(?<![\w.\-@])({_LBL}{_TCH}*?)([{_HYPHENS_SP}])[ \t]+({_LBL}{_TCH}*)", re.I)
# разрыв у точки перед зоной: «example.⏎ru», «example⏎.ru», «example. ru»
_WRAP_DOT = re.compile(rf"(?<![\w.\-@])({_LBL}{_TCH}*?{_LBL})(?:\.[ \t]*\r?\n\s*|\.[ \t]+|[ \t]*\r?\n\s*\.)"
                       rf"({_TLD_ALT})(?![\w\-])", re.I)
_FULL_DOMAIN = re.compile(rf"(?:{_LBL}[a-zа-яё0-9\-]*\.)+(?:{_TLD_ALT})\.?", re.I)


def join_wrapped_domains(text: str, joined: list | None = None) -> str:
    """Склеивает доменные имена, разорванные переносом строки (правила — выше). В joined (если передан)
    добавляются склеенные имена — их стоит показать оператору."""
    text = text or ""

    def hyphen(m: re.Match) -> str:
        left, dash, right = m.group(1), m.group(2), m.group(3)
        if _FULL_DOMAIN.fullmatch(left):
            return m.group(0)          # слева уже полное имя — это не перенос
        sep = "" if dash == "\u00ad" else "-"   # мягкий перенос в имя не входит
        if joined is not None:
            joined.append((left + sep + right).lower().strip(".-"))
        return left + sep + right

    def dot(m: re.Match) -> str:
        if _FULL_DOMAIN.fullmatch(m.group(1)):
            return m.group(0)          # «example.ru.⏎Info …» — конец предложения, а не разрыв имени
        if joined is not None:
            joined.append(f"{m.group(1)}.{m.group(2)}".lower())
        return f"{m.group(1)}.{m.group(2)}"

    for _ in range(5):                 # имя, разорванное дважды, склеивается за два прохода
        new = _WRAP_HYPHEN_NL.sub(hyphen, text)
        new = _WRAP_HYPHEN_SP.sub(hyphen, new)
        new = _WRAP_DOT.sub(dot, new)
        if new == text:
            break
        text = new
    return text.replace("\u00ad", "")


_COUNT_WORDS = [
    ("девятьсот", 900), ("восемьсот", 800), ("семьсот", 700), ("шестьсот", 600), ("пятьсот", 500),
    ("четыреста", 400), ("триста", 300), ("двести", 200), ("сто", 100),
    ("девяносто", 90), ("восемьдесят", 80), ("семьдесят", 70), ("шестьдесят", 60), ("пятьдесят", 50),
    ("сорок", 40), ("тридцат", 30), ("двадцат", 20),
    ("девятнадцат", 19), ("восемнадцат", 18), ("семнадцат", 17), ("шестнадцат", 16), ("пятнадцат", 15),
    ("четырнадцат", 14), ("тринадцат", 13), ("двенадцат", 12), ("одиннадцат", 11), ("десят", 10),
    ("девят", 9), ("восем", 8), ("сем", 7), ("шест", 6), ("пят", 5), ("четыр", 4), ("три", 3),
    ("два", 2), ("две", 2), ("один", 1), ("одн", 1),
]


def _count_from_words(s: str) -> int | None:
    total = 0
    for w in re.findall(r"[а-яё]+", s.lower()):
        v = next((n for stem, n in _COUNT_WORDS if w.startswith(stem)), None)
        if v is None:
            return None
        total += v
    return total or None


def stated_domain_count(text: str) -> int | None:
    """Сколько доменов заявитель указал сам: «— всего 76 (семьдесят шесть) доменов», «в количестве 3 шт.»."""
    t = (text or "").lower().replace("ё", "е")
    m = (re.search(r"(?:всего|итого|в\s+количестве|количество\s+доменов)\s*[:\-—–]?\s*(\d{1,4})(?!\d|\.\w)", t)
         or re.search(r"(?<![\w.\-])(\d{1,4})\s*(?:\([^)]{0,60}\)\s*)?(?:шт\.?\s*)?домен", t))
    if m:
        return int(m.group(1))
    m = re.search(r"всего\s+((?:[а-я]+\s+){1,4}?)\(?\s*домен", t)
    return _count_from_words(m.group(1)) if m else None


@dataclass
class DomainList:
    """Разбор списка доменов из заявления."""
    domains: list[str] = field(default_factory=list)
    joined: list[str] = field(default_factory=list)        # собраны из частей на разных строках
    fragments: list[str] = field(default_factory=list)     # обрывки без доменной зоны («business», «инфоклуб.»)
    mixed: list[str] = field(default_factory=list)         # кириллица и латиница в одном имени (ошибка OCR?)
    stated_count: int | None = None                        # «всего N доменов» в тексте заявления

    @property
    def count_mismatch(self) -> bool:
        return self.stated_count is not None and self.stated_count != len(self.domains)


def _known_word(w: str) -> bool:
    from .names import _morph   # словарь pymorphy3 (уже загружен при работе программы)
    return any(p.is_known for p in _morph.parse(w))


def _domain_fragments(text: str, domains: list[str]) -> list[str]:
    """Куски текста, похожие на часть доменного имени, которые не вошли ни в один найденный домен."""
    out: list[str] = []
    dset = set(domains)
    alnum = lambda x: re.sub(r"[^a-zа-яё0-9]", "", x)  # noqa: E731
    dalnum = {alnum(d) for d in domains}
    for tok in re.split(r"[\s,;]+", text or ""):
        t = tok.strip("()[]{}«»\"'“”„:!?|").lower()
        core = t.strip(".-\u2010\u2011–—")
        if len(core) < 2 or "@" in t or "/" in t or re.fullmatch(r"[\d.,\-]+", core):
            continue
        # входит в найденный домен (в т. ч. исправленный: рукописное «distobR-RU» → distobr.ru)
        if core in dset or any(d in t for d in domains) or alnum(core) in dalnum:
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", core) and re.search(r"[a-z]", core):
            if core not in ("www", "http", "https"):
                out.append(t)          # латиница без зоны: «business», «info-», «example.»
        elif re.fullmatch(r"[а-яё0-9][а-яё0-9.\-]*", core) and re.search(r"[а-яё]", core):
            cut = t.endswith((".", "-")) and len(core) >= 3 and ("-" in core or not _known_word(core))
            odd_zone = re.fullmatch(r"[а-яё0-9\-]{2,}\.[а-яё]{2,}", core) is not None
            if cut or odd_zone:
                out.append(t)          # «инфоклуб.» — зона не прочитана; «инфо-клуб.рр» — неизвестная зона
    return list(dict.fromkeys(out))


def parse_domain_list(text: str) -> DomainList:
    """Домены из поля заявления + признаки того, что список прочитан не целиком."""
    joined: list[str] = []
    s = join_wrapped_domains(text or "", joined)
    doms = find_domains(s)
    flat = re.sub(r"\s+", " ", s)
    return DomainList(
        domains=doms,
        joined=[d for d in doms if d in joined],
        fragments=_domain_fragments(flat, doms),
        mixed=[d for d in doms if re.search(r"[a-z]", d) and re.search(r"[а-яё]", d)],
        stated_count=stated_domain_count(flat),
    )
