"""Работа с ФИО: падежи (pymorphy3), словарная проверка имён/отчеств, сравнение."""
from __future__ import annotations

import re
from functools import lru_cache

import pymorphy3
from rapidfuzz import fuzz

_morph = pymorphy3.MorphAnalyzer()
_NAME_TAGS = {"Name", "Surn", "Patr"}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").upper().replace("Ё", "Е")).strip(" .,")


@lru_cache(maxsize=4096)
def is_known(word: str, tag: str) -> bool:
    """Есть ли слово в словаре как имя (Name) / отчество (Patr) / фамилия (Surn)."""
    return any(tag in p.tag and p.is_known for p in _morph.parse(word.lower()))


def gender_from_patronymic(patr: str) -> str | None:
    p = norm(patr)     # в любом падеже: «Николаевича», «Михайловны»
    if re.search(r"(?:ВИЧ|ИЧ)(?:А|У|ЕМ|Е)?$|ОГЛЫ$|УУЛУ$", p):
        return "masc"
    if re.search(r"(?:ВН|ИЧН)(?:А|Ы|Е|У|ОЙ)$|КЫЗЫ$", p):
        return "femn"
    return None


@lru_cache(maxsize=4096)
def nominative_variants(word: str, gender: str | None = None) -> frozenset[str]:
    """Все правдоподобные формы им. падежа для слова из ФИО (в любом падеже)."""
    w = word.lower()
    out = {norm(word)}
    for p in _morph.parse(w):
        if not (_NAME_TAGS & set(p.tag.grammemes)) and p.tag.POS not in ("NOUN", "ADJF", "ADJS"):
            continue
        if gender and p.tag.gender and p.tag.gender != gender:
            continue
        f = p.inflect({"sing", "nomn"})
        if f:
            out.add(norm(f.word))
    return frozenset(out)


def split_fio(s: str) -> list[str]:
    s = re.sub(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}", " ", s or "")
    return [t for t in re.findall(r"[А-ЯЁа-яё\-]+", s) if len(t) > 1 or t.isupper()]


# отчество в любом падеже: Викторович/Викторовича/…, Викторовна/Викторовны/…, оглы, кызы
_PATR_RE = re.compile(r"(?:ВИЧ|ИЧ)(?:А|У|ЕМ|Е)?$|(?:ВН|ИЧН)(?:А|Ы|Е|У|ОЙ)$|^(?:ОГЛЫ|КЫЗЫ|УУЛУ)$")


def is_patronymic(word: str) -> bool:
    return bool(_PATR_RE.search(norm(word)))


def ordered(toks: list[str]) -> list[str]:
    """«Виктор Дмитриевич Андреев» (Имя Отчество Фамилия) → «Андреев Виктор Дмитриевич».
    Порядок определяется по отчеству: оно второе из трёх слов, а последнее слово — не отчество."""
    if len(toks) == 3 and is_patronymic(toks[1]) and not is_patronymic(toks[2]):
        return [toks[2], toks[0], toks[1]]
    return list(toks)


# слова, на которых ФИО заканчивается: «ШЕЙКО ЮРИЙ БОРИСОВИЧ АПРЕЛЯ ГОДА», «… прошу передать права»
_FIO_STOP = re.compile(
    r"^(?:(?:ЯНВАР|ФЕВРАЛ|АПРЕЛ|ИЮН|ИЮЛ|СЕНТЯБР|ОКТЯБР|НОЯБР|ДЕКАБР)[ЬЯЕ]|МАРТА?|МАРТЕ|МА[ЯЙЕ]|АВГУСТА?|АВГУСТЕ|"
    r"ГОДА?|ГОДУ|ГР|РОЖДЕНИЯ|ПАСПОРТА?|ПРОШУ|ПРОСИМ|ПРОСИТ|ПЕРЕДАТЬ|ПЕРЕДАЧЕ|ПРАВА?|ДАТА|СЕРИ[ЯИ]|НОМЕР|ВЫДАН[АО]?|"
    r"ИНН|ПОЛ|МЕСТО|ГРАЖДАН(?:ИН|КА|КИ|ИНА|СТВО)|ЗАРЕГИСТРИРОВАН\w*|ПРОЖИВАЮЩ\w*|АДРЕС|ДОМЕН\w*|ФИО|КОД|СНИЛС|ОГРН|"
    r"ТЕЛ|ТЕЛЕФОН)$")
_FIO_LEAD = re.compile(r"^(?:ГРАЖДАН\w*|ГР|РОССИЙСКОЙ|ФЕДЕРАЦИИ|РФ|ИП)$")


def extract_fio(s: str, max_words: int = 4) -> list[str]:
    """ФИО из начала строки: «гражданки Российской Федерации, Джолдасовой Анастасии Борисовны, пол женский, …» →
    [Джолдасовой, Анастасии, Борисовны]. Останавливается на слове, которое не может быть частью ФИО:
    со строчной буквы («прошу», «пол»), месяц, «года», «рождения», «паспорт» и т. п. Порядок слов — как в тексте."""
    toks = split_fio(s)
    out: list[str] = []
    for t in toks:
        u = norm(t)
        if not out and _FIO_LEAD.match(u):
            continue
        if _FIO_STOP.match(u) or (not t[0].isupper() and u not in ("ОГЛЫ", "КЫЗЫ", "УУЛУ")):
            if out:
                break
            continue
        out.append(t)
        if len(out) >= max_words or (len(out) >= 3 and is_patronymic(t)):
            break
    return out


def to_nominative(fio: str) -> str:
    """«Поповой Екатерины Сергеевны» -> «ПОПОВА ЕКАТЕРИНА СЕРГЕЕВНА» (лучший вариант)."""
    toks = ordered(split_fio(fio))
    if not toks:
        return ""
    g = gender_from_patronymic(toks[-1]) if len(toks) >= 3 else None
    res = []
    for i, t in enumerate(toks):
        tag = "Surn" if i == 0 else "Name" if i == 1 else "Patr"
        best = None
        for p in _morph.parse(t.lower()):
            if tag in p.tag and (not g or p.tag.gender in (g, None)):
                f = p.inflect({"sing", "nomn"})
                if f:
                    best = f.word
                    break
        if best is None and i == 0:
            best = _surname_nominative(t, g)
        res.append(norm(best or t))
    if g == "femn" and res and res[0].endswith(("ОВ", "ЕВ", "ИН", "ЫН")):
        res[0] += "А"  # фамилии на -ов/-ин у женщин
    return " ".join(res)


def _surname_nominative(word: str, gender: str | None) -> str | None:
    """Фамилия на -ов/-ев/-ин не из словаря в косвенном падеже: «Крайнева» (муж., род.) → «Крайнев»,
    «Дымовой» (жен.) → «Дымова»."""
    u = norm(word)
    if gender == "masc":
        for end, cut in (("ОВА", 1), ("ЕВА", 1), ("ЁВА", 1), ("ИНА", 1), ("ЫНА", 1), ("ОВУ", 1), ("ЕВУ", 1),
                         ("ИНУ", 1), ("ОВЫМ", 2), ("ЕВЫМ", 2), ("ИНЫМ", 2)):
            if u.endswith(end):
                return u[:-cut]
    if gender == "femn":
        for end in ("ОВОЙ", "ЕВОЙ", "ЁВОЙ", "ИНОЙ", "ЫНОЙ"):
            if u.endswith(end):
                return u[:-2] + "А"
        for end in ("ОВУ", "ЕВУ", "ИНУ"):
            if u.endswith(end):
                return u[:-1] + "А"
    return None


def fio_similarity(a: str, b: str) -> tuple[float, list[str]]:
    """Сравнение ФИО с учётом падежей. Возвращает (0..100, пояснения по частям)."""
    ta, tb = ordered(split_fio(a)), ordered(split_fio(b))
    if not ta or not tb:
        return 0.0, ["пустое значение"]
    ga = gender_from_patronymic(ta[-1]) if len(ta) >= 3 else None
    scores, notes = [], []
    labels = ["фамилия", "имя", "отчество"]
    for i in range(max(len(ta), len(tb))):
        if i >= len(ta) or i >= len(tb):
            scores.append(0)
            notes.append(f"{labels[i] if i < 3 else 'часть'}: отсутствует")
            continue
        cands = nominative_variants(ta[i], ga) | {norm(ta[i])}
        target = norm(tb[i])
        best = max(fuzz.ratio(c, target) for c in cands)
        # инициалы: «Попова Е.С.»
        if len(ta[i]) == 1 and target.startswith(norm(ta[i])):
            best = 100
        scores.append(best)
        if best < 100:
            notes.append(f"{labels[i] if i < 3 else 'часть'}: «{ta[i]}» vs «{tb[i]}» ({best:.0f}%)")
    return sum(scores) / len(scores), notes


def ocr_typo_only(app_fio: str, pas_fio: str) -> bool:
    """Расхождение только в имени/отчестве, где в паспорте прочитано несловарное слово,
    а в заявлении — словарное и очень похожее («РИКТОРОВИЧ» vs «Викторович»): вероятная ошибка OCR паспорта."""
    ta, tb = split_fio(app_fio), split_fio(pas_fio)
    if len(ta) != len(tb) or len(ta) < 2:
        return False
    g = gender_from_patronymic(ta[-1]) if len(ta) >= 3 else None
    any_diff = False
    for i, (a, b) in enumerate(zip(ta, tb)):
        cands = nominative_variants(a, g) | {norm(a)}
        if norm(b) in cands:
            continue
        if i == 0:
            return False  # фамилию по словарю не проверить
        tag = "Name" if i == 1 else "Patr"
        best = max(cands, key=lambda c: fuzz.ratio(c, norm(b)))
        if fuzz.ratio(best, norm(b)) < 85 or is_known(b, tag) or not is_known(best, tag):
            return False
        any_diff = True
    return any_diff
