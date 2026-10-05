"""Извлечение данных с главного разворота (стр. 2–3) паспорта гражданина РФ.

Источники в порядке надёжности:
1. MRZ (если есть и сошлись контрольные цифры) — ФИО, дата рождения, пол, серия/номер,
   дата выдачи, код подразделения;
2. OCR красных цифр серии/номера (поворот + цветовая маска);
3. OCR основного текста (несколько проходов Tesseract, эвристики по расположению);
4. опционально — локальная VLM (Ollama) для недостающих полей.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date

import cv2
import numpy as np
from rapidfuzz import fuzz

from . import mrz as mrzlib
from .models import Extraction
from .issuer import correct_ocr, vocab_share
from .names import is_known
from .progress import log
from . import parsers
from .ocr import (Line, OllamaVLM, Page, best_orientation, crop_to_content, downscale, rotate,
                  straighten_photo, tesseract_lines, tesseract_text)

KEYWORDS = ["РОССИЙСКАЯ", "ФЕДЕРАЦИЯ", "ПАСПОРТ", "ВЫДАН", "ФАМИЛИЯ", "ОТЧЕСТВО",
            "РОЖДЕНИЯ", "ПОДРАЗДЕЛЕНИЯ", "МУЖ", "ЖЕН", "ДАТА", "ИМЯ"]
STOP_WORDS = {"РОССИЙСКАЯ", "ФЕДЕРАЦИЯ", "МУЖ", "ЖЕН", "ГОР", "ОБЛ", "ПАСПОРТ", "ВЫДАН",
              "ФАМИЛИЯ", "ИМЯ", "ОТЧЕСТВО", "ДАТА", "МЕСТО", "РОЖДЕНИЯ", "ЛИЧНЫЙ", "КОД",
              "ПОДРАЗДЕЛЕНИЯ", "ПОДПИСЬ", "ЛИЧНАЯ", "ВЫДАЧИ", "ПОЛ"}
PATRONYMIC_RE = re.compile(r"(ВИЧ|ВНА|ИЧНА|ИНИЧНА|ОГЛЫ|КЫЗЫ|УУЛУ)$")
DATE_RE = re.compile(r"(?<!\d)(\d{2})\s?[.,·\-]\s?(\d{2})\s?[.,·\-]\s?((?:19|20)\d{2})(?!\d)")
DEPT_RE = re.compile(r"(?<!\d)(\d{3})\s?[-—–=]\s?(\d{3})(?!\d)")
SERIES_RE = re.compile(r"(?<!\d)(\d{2})\s?(\d{2})\s?(\d{6})(?!\d)")
UPPER_TOKEN_RE = re.compile(r"[А-ЯЁ][А-ЯЁ\-]{2,}")


# ------------------------------------------------------------------ предобработка

def flatten(channel: np.ndarray) -> np.ndarray:
    """Выравнивание освещённости: деление на размытый фон."""
    small = cv2.resize(channel, None, fx=0.25, fy=0.25)
    bg = cv2.medianBlur(small, 31)
    bg = cv2.resize(bg, (channel.shape[1], channel.shape[0]))
    return cv2.divide(channel, bg, scale=255)


def variants(img: np.ndarray) -> dict[str, np.ndarray]:
    # В красном канале исчезают розовый фон, защитная сетка и красные номера,
    # а чёрный/фиолетовый текст остаётся тёмным.
    return {"red": flatten(img[:, :, 2]),
            "gray": flatten(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))}


def red_ink_mask(img: np.ndarray, sat: int) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (0, sat, 50), (12, 255, 255)) | cv2.inRange(hsv, (160, sat, 50), (180, 255, 255))
    return 255 - m


# ------------------------------------------------------------------ выбор страницы

def find_main_spread(pages: list[Page], max_pages: int = 4) -> tuple[Page, int, int]:
    """Ищет главный разворот среди первых страниц. Белые поля листа обрезаются заранее:
    паспорт часто занимает половину A4, и иначе мелкие подписи теряются при уменьшении."""
    best = (pages[0], 0, -1)
    for p in pages[:max_pages]:
        if p.image is None:
            continue
        img, cropped = crop_to_content(p.image)
        q = Page(image=img, text_layer=p.text_layer, source=p.source, index=p.index) if cropped else p
        angle, score = best_orientation(q.image, KEYWORDS)
        if score > best[2]:
            best = (q, angle, score)
        if score >= 5:
            break
    return best


# ------------------------------------------------------------------ MRZ

def _line1_score(l1: str) -> int:
    return (3 * l1.startswith("PNRUS") + 2 * l1.startswith("P") + 2 * ("<<" in l1[5:])
            - len(re.findall(r"K{2,}", l1)) - l1[5:20].count("K<"))


def read_mrz(img: np.ndarray) -> mrzlib.MRZResult | None:
    """OCR нижней части разворота в нескольких вариантах; строка 2 выбирается по контрольным
    цифрам, строка 1 (ФИО) — по «правдоподобию»."""
    h = img.shape[0]
    region = img[int(h * 0.7):, :]
    wl = "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
    pairs = []
    for name, v in variants(region).items():
        for scale in (1.0, 1.5):
            vv = cv2.resize(v, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale != 1 else v
            lines = tesseract_lines(vv, lang="eng", psm=6, config=wl)
            found = mrzlib.find_mrz_lines([l.text for l in lines])
            if found:
                pairs.append(found)
    if not pairs:
        return None
    results = [mrzlib.parse_td3(l1, l2) for l1, l2 in pairs]
    best2 = max(results, key=lambda r: (sum(r.checks.values()), -r.corrections))
    best1 = max(pairs, key=lambda p: _line1_score(p[0]))[0]
    return mrzlib.parse_td3(best1, best2.raw[1])


# ------------------------------------------------------------------ серия и номер

def read_series_number(img: np.ndarray) -> list[str]:
    found = []
    wl = "-c tessedit_char_whitelist=0123456789"
    for sat in (60, 35):
        m = red_ink_mask(img, sat)
        for angle in (270, 90, 0):
            txt, _ = tesseract_text(rotate(m, angle), lang="eng", psm=11, config=wl)
            s = " ".join(txt.split())
            for a, b, c in SERIES_RE.findall(s):
                found.append(f"{a}{b} {c}")
    return found


# ------------------------------------------------------------------ основной текст

def _clean_upper(text: str) -> str:
    text = text.upper().replace("Ё", "Е")
    toks = re.findall(r"[А-Я][А-Я\.\-]*", text)
    toks = [t for t in toks if len(t.strip(".-")) >= 2 or t in ("В", "И")]
    return " ".join(toks)


def _uppercase_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if letters else 0


def ocr_passes(img: np.ndarray, extra_upscale: bool | None = None) -> list[tuple[str, Line]]:
    out = []
    for vname, v in variants(img).items():
        for psm in (3, 11):
            for l in tesseract_lines(v, psm=psm):
                if len(l.text.strip()) >= 2:
                    out.append((f"{vname}/psm{psm}", l))
    if extra_upscale or (extra_upscale is None and max(img.shape[:2]) < 2600):
        # фото с телефона: дополнительный проход по увеличенному изображению
        big = cv2.resize(variants(img)["gray"], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        for l in tesseract_lines(big, psm=11):
            for w in l.words:
                w.x, w.y, w.w, w.h = w.x // 2, w.y // 2, w.w // 2, w.h // 2
            if len(l.text.strip()) >= 2:
                out.append(("gray-x2/psm11", l))
    return out


def _vote(values: list[tuple[str, float]]) -> tuple[str, float] | None:
    """Голосование по нормализованным значениям: частота + средняя уверенность."""
    if not values:
        return None
    cnt = Counter(v for v, _ in values)
    best = max(cnt, key=lambda v: (cnt[v], np.mean([c for x, c in values if x == v])))
    conf = np.mean([c for x, c in values if x == best]) / 100
    agreement = cnt[best] / len(values)
    return best, float(min(0.95, conf * (0.6 + 0.4 * agreement)))


def parse_main_text(lines: list[tuple[str, Line]], img_h: int) -> dict:
    res: dict = {}
    dates, depts, sexes = [], [], []
    for _, l in lines:
        t = l.text
        y = l.box[1] / img_h
        for d, m, yy in DATE_RE.findall(t):
            try:
                dt = date(int(yy), int(m), int(d))
            except ValueError:
                continue
            ctx = t.lower()
            kind = "issue" if "выда" in ctx else "birth" if ("рожд" in ctx or "пол" in ctx) else ""
            dates.append((dt, y, kind, l.conf))
        for a, b in DEPT_RE.findall(t):
            depts.append((f"{a}-{b}", l.conf))
        up = t.upper()
        if re.search(r"\bМУ[ЖХK]\b", up):
            sexes.append(("МУЖ", l.conf))
        elif re.search(r"\bЖЕН\b", up):
            sexes.append(("ЖЕН", l.conf))

    # даты: дата выдачи — в верхней половине разворота, дата рождения — в нижней
    issue = [(d.strftime("%d.%m.%Y"), c) for d, y, k, c in dates if k == "issue" or (k == "" and y < 0.5)]
    birth = [(d.strftime("%d.%m.%Y"), c) for d, y, k, c in dates if k == "birth" or (k == "" and y >= 0.5)]
    if (v := _vote(issue)):
        res["issue_date"] = v
    if (v := _vote(birth)):
        res["birth_date"] = v
    if (v := _vote(depts)):
        res["department_code"] = v
    if (v := _vote(sexes)):
        res["sex"] = v

    # ФИО: заглавные слова нижней половины; отчество определяется по суффиксу
    tokens = []
    for _, l in lines:
        if l.box[1] / img_h < 0.45:
            continue
        for w in l.words:
            for tok in UPPER_TOKEN_RE.findall(w.text.replace("Ё", "Е")):
                if tok not in STOP_WORDS and len(tok) >= 3 and _uppercase_ratio(w.text) > 0.8:
                    tokens.append((tok, w.cy / img_h, w.conf))
    strong = [t for t in tokens if t[2] >= 40]
    tokens = strong or tokens
    patr = [(t, y, c) for t, y, c in tokens if PATRONYMIC_RE.search(t)]
    if patr:
        p_val = _wvote([(t, c) for t, _, c in patr], "Patr")
        p_y = float(np.median([y for t, y, _ in patr if t == p_val[0]]))
        res["patronymic"] = p_val
        above = sorted([(t, y, c) for t, y, c in tokens if p_y - 0.2 < y < p_y - 0.012], key=lambda x: x[1])
        groups: list[list] = []
        for t, y, c in above:
            if groups and abs(groups[-1][-1][1] - y) < 0.015:
                groups[-1].append((t, y, c))
            else:
                groups.append([(t, y, c)])
        if len(groups) >= 1:
            res["given_name"] = _wvote([(t, c) for t, _, c in groups[-1]], "Name")
        if len(groups) >= 2:
            res["surname"] = _wvote([(t, c) for t, _, c in groups[-2]])
        below = _clean_lines(lines, img_h, p_y + 0.03, p_y + 0.16)
        if below:
            res["birth_place"] = (_merge_lines(below), float(np.mean([c for *_, c in below]) / 100 * 0.8))

    # кем выдан: заглавные строки между «РОССИЙСКАЯ ФЕДЕРАЦИЯ» и датой выдачи
    fed_y = min([l.box[3] / img_h for _, l in lines
                 if fuzz.partial_ratio("ФЕДЕРАЦИЯ", l.text.upper()) > 75 and l.box[1] / img_h < 0.3] or [0.05])
    issue_y = min([y for d, y, k, c in dates if k == "issue" or y < 0.5] or [0.4])
    issuer = _clean_lines(lines, img_h, fed_y - 0.005, issue_y - 0.005)
    if issuer:
        res["issued_by"] = (_merge_lines(issuer, vocab=True), float(np.mean([c for *_, c in issuer]) / 100 * 0.8))
    return res


LABELS = ["ПАСПОРТ", "ВЫДАН", "ЛИЧНЫЙ", "ПОДПИСЬ", "ЛИЧНАЯ", "МЕСТО", "РОЖДЕНИЯ", "ДАТА",
          "ВЫДАЧИ", "ПОДРАЗДЕЛЕНИЯ", "РОССИЙСКАЯ", "ФЕДЕРАЦИЯ", "ФАМИЛИЯ", "ОТЧЕСТВО"]
SHORT_OK = {"В", "И", "ПО", "Г.", "Г", "ГОР.", "ОБЛ.", "Р-НА", "Р-НЕ", "ОВД", "МВД", "УВД", "ГУ",
            "ОУФМС", "УФМС", "ТП", "ОТД.", "С.", "П.", "ПОС.", "ДЕР.", "РОВД", "ГОМ", "АО", "РЕСП."}


def _clean_lines(lines, img_h: int, y_from: float, y_to: float) -> list[tuple[float, str, float]]:
    """Строки значений (заглавными) в диапазоне по высоте, без подписей полей и мусора."""
    out = []
    for _, l in lines:
        y0 = l.box[1] / img_h
        if not (y_from < y0 < y_to) or DATE_RE.search(l.text):
            continue
        words = []
        for w in l.words:
            t = re.sub(r"[^А-ЯЁA-Z\.\-]", "", w.text.upper()).replace("Ё", "Е").strip("-")
            if not t or _uppercase_ratio(w.text) < 0.8 or w.conf < 30:
                continue
            if any(fuzz.ratio(t.strip("."), lab) > 80 for lab in LABELS) or re.fullmatch(r"(МУ[ЖХ]|ЖЕН)\.?", t):
                continue
            if len(t) >= 4 and any(fuzz.partial_ratio(t.strip("."), lab) >= 95 for lab in ("ФЕДЕРАЦИЯ", "РОССИЙСКАЯ")):
                continue  # обрывки заголовка «РОССИЙСКАЯ ФЕДЕРАЦИЯ»
            if len(t.strip(".")) <= 3 and t not in SHORT_OK:
                continue
            words.append((t, w.conf))
        if words:
            out.append((round(y0, 3), " ".join(t for t, _ in words), float(np.mean([c for _, c in words]))))
    return out


def _wvote(values: list[tuple[str, float]], tag: str | None = None) -> tuple[str, float]:
    """Голосование с весом = уверенность OCR; слова из словаря имён/отчеств получают приоритет."""
    acc: dict[str, float] = {}
    for v, c in values:
        bonus = 3 if tag and is_known(v, tag) else 1
        acc[v] = acc.get(v, 0) + max(c, 1) * bonus
    best = max(acc, key=acc.get)
    share = acc[best] / sum(acc.values())
    conf = np.mean([c for v, c in values if v == best]) / 100
    return best, float(min(0.95, conf * (0.6 + 0.4 * share)))


def _merge_lines(items: list[tuple[float, str, float]], vocab: bool = False) -> str:
    """Склеивает строки разных проходов OCR: для каждой позиции по y выбирается прочтение,
    с которым согласны остальные проходы (вес — уверенность × длина). vocab=True — для «кем выдан»:
    прочтения из слов названий органов («ОБЛАСТИ», «РОССИИ») получают приоритет над случайными
    словами («ВЛАСТИ»)."""
    items = sorted(items)
    rows: list[list] = []
    for y, txt, c in items:
        if rows and abs(rows[-1][0][0] - y) < 0.012:
            rows[-1].append((y, txt, c))
        else:
            rows.append([(y, txt, c)])
    out = []
    for r in rows:
        def score(x):
            s = sum(fuzz.ratio(x[1], o[1]) / 100 * o[2] * min(len(o[1]), 40) for o in r)
            return s * (0.5 + vocab_share(x[1])) if vocab else s
        y, txt, c = max(r, key=score)
        if not any(fuzz.partial_ratio(txt, o) > 85 for o in out):
            out.append(txt)
    return " ".join(out)


# ------------------------------------------------------------------ VLM

VLM_PROMPT_TOP = (
    "Это верхняя страница разворота паспорта гражданина РФ (кем выдан, дата выдачи, код подразделения). "
    "Текст может быть написан от руки. Верни JSON с ключами: issued_by, issue_date, department_code. "
    "Дату пиши в формате ДД.ММ.ГГГГ ровно так, как написано в документе. Если поле не видно — null. Только JSON."
)
VLM_PROMPT_BOTTOM = (
    "Это нижняя страница разворота паспорта гражданина РФ (страница с фотографией). "
    "Текст может быть написан от руки. Верни JSON с ключами: surname, given_name, patronymic, sex, "
    "birth_date, birth_place. Дату пиши в формате ДД.ММ.ГГГГ ровно так, как написано. "
    "Если поле не видно — null. Только JSON."
)


def _json(out: str) -> dict:
    m = re.search(r"\{.*\}", out, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def vlm_extract(img: np.ndarray, vlm: OllamaVLM) -> dict:
    """Модель читает разворот по половинам: так у каждой страницы вдвое больше пикселей,
    чем при отправке всего разворота (рукописные даты на мелкой картинке модель додумывает)."""
    h = img.shape[0]
    top, bottom = img[: int(h * 0.52)], img[int(h * 0.48):]
    data = {}
    data.update(_json(vlm.ask(downscale(top, 1400), VLM_PROMPT_TOP, max_tokens=200)))
    data.update(_json(vlm.ask(downscale(bottom, 1400), VLM_PROMPT_BOTTOM, max_tokens=200)))
    return data


# ------------------------------------------------------------------ правдоподобие значений

PASSPORT_START = date(1997, 10, 1)   # начало выдачи паспортов РФ этого образца
# Год в серии (3–4 цифры) — год изготовления бланка. Бланки печатают с опережением, поэтому паспорт
# с серией «46 19» может быть выдан в 2018 г. (набор 7 — Опря, подтверждено MRZ). Допуск — 3 года.
SERIES_YEAR_LEAD = 3
TRUSTED_SOURCES = ("mrz", "manual")   # значения, которые не отбрасываются: MRZ с контрольными цифрами, ввод оператора


def blank_year(series_number: str | None) -> int | None:
    sn = re.sub(r"\D", "", series_number or "")
    if len(sn) < 4:
        return None
    yy = int(sn[2:4])
    return 2000 + yy if yy < 90 else 1900 + yy


def plausibility(ex: Extraction) -> list[str]:
    """Проверяет значения паспорта на правдоподобие. Невозможные значения (дата выдачи 1987 г.,
    выдача до 14 лет и т. п.) убираются в «альтернативы» и помечаются для ручной проверки.
    Значения из MRZ не отбрасываются: контрольные цифры подтверждают, что так напечатано в паспорте;
    странности в них отражаются в проверках («Серия ↔ год выдачи», «Срок действия»)."""
    problems = []
    g = ex.get
    bd = parsers.parse_date(g("birth_date") or "")
    iss = parsers.parse_date(g("issue_date") or "")
    by = blank_year(g("series_number"))
    today = date.today()

    def reject(key, why):
        f = ex.fields.get(key)
        if f is None:
            return
        if f.source in TRUSTED_SOURCES:
            problems.append(f"{FIELD_RU.get(key, key)}: «{f.value}» — {why} (значение из {f.source.upper()}, оставлено)")
            return
        problems.append(f"{FIELD_RU.get(key, key)}: «{f.value}» — {why}; значение отброшено, нужна ручная проверка")
        ex.fields[key] = type(f)("", 0.0, f.source, raw=f.raw, needs_review=True,
                                 alternatives=[f.value] + list(f.alternatives))

    if bd and not (date(1900, 1, 1) <= bd <= date(today.year - 14, today.month, min(today.day, 28))):
        reject("birth_date", "невозможная дата рождения")
        bd = None
    if iss:
        if iss < PASSPORT_START:
            reject("issue_date", "паспорта РФ этого образца выдаются с 01.10.1997")
        elif iss > today:
            reject("issue_date", "дата выдачи в будущем")
        elif bd and (iss - bd).days < 14 * 365:
            reject("issue_date", "раньше 14-летия владельца")
        elif by and iss.year < by - SERIES_YEAR_LEAD:
            reject("issue_date", f"на {by - iss.year} г. раньше года бланка по серии ({by})")
    dc = g("department_code")
    if dc and not re.fullmatch(r"\d{3}-\d{3}", dc):
        reject("department_code", "неверный формат кода подразделения")
    return problems


FIELD_RU = {"birth_date": "Дата рождения", "issue_date": "Дата выдачи", "department_code": "Код подразделения",
            "series_number": "Серия и номер"}


# ------------------------------------------------------------------ главная функция

def extract_passport(pages: list[Page], vlm: OllamaVLM | None = None, mode: str = "auto") -> Extraction:
    """mode: auto | scan | photo."""
    ex = Extraction(doc_type="passport_rf", source_file=pages[0].source)
    log(f"Паспорт: ищу разворот с фото (страниц в файле: {len(pages)})…")
    page, angle, score = find_main_spread(pages)
    log(f"Паспорт: разворот — стр. {page.index + 1}, поворот {angle}°")
    img = rotate(page.image, angle)
    if mode == "photo":
        img2, ok = straighten_photo(img)
        if ok:
            img = img2
            log("Паспорт (фото): перспектива выровнена")
    ex.debug.update(page=page.index + 1, rotation=angle, keyword_score=score)
    if score < 3:
        ex.notes.append("Главный разворот паспорта найден неуверенно — проверьте вручную.")

    # 1. основной текст
    log("Паспорт: распознаю текст (несколько проходов OCR)…")
    lines = ocr_passes(img, extra_upscale=True if mode == "photo" else None)
    parsed = parse_main_text(lines, img.shape[0])
    for k, (v, c) in parsed.items():
        ex.set(k, v, c, "ocr")
    if "issued_by" in ex.fields:  # опечатки OCR в словах названия органа: «РООСИИ» → «РОССИИ»
        f = ex.fields["issued_by"]
        fixed, fixes = correct_ocr(f.value)
        fixed = fixed.strip(" ,")
        if re.search(r"[А-Я]{5,}\.$", fixed):  # «ОБЛАСТИ.» — точка-мусор; «ОБЛ.» — сокращение, оставляем
            fixed = fixed[:-1]
        if fixes:
            ex.debug["issued_by_fixes"] = fixes
            f.alternatives = [f.value] + list(f.alternatives)
            f.raw = f.value
        f.value = fixed

    # 2. серия и номер красными цифрами
    log("Паспорт: серия и номер…")
    sn = read_series_number(img)
    if sn:
        ranked = Counter(sn).most_common()
        v, n = ranked[0]
        tie = len(ranked) > 1 and ranked[1][1] == n
        ex.set("series_number", v, 0.45 if tie else min(0.95, 0.6 + 0.1 * n), "red-ocr",
               raw="; ".join(x for x, _ in ranked))
        ex.fields["series_number"].alternatives = [x for x, _ in ranked]

    # 3. MRZ
    log("Паспорт: машиночитаемая зона (MRZ)…")
    m = read_mrz(img)
    if m:
        ex.debug["mrz"] = m.to_dict()
        if m.valid:
            ex.set("surname", m.surname, 0.99, "mrz")
            ex.set("given_name", m.given_names, 0.99, "mrz")
            ex.set("patronymic", m.patronymic, 0.99, "mrz")
            ex.set("birth_date", m.birth_date.strftime("%d.%m.%Y") if m.birth_date else None, 0.99, "mrz")
            ex.set("sex", {"М": "МУЖ", "Ж": "ЖЕН"}.get(m.sex, m.sex), 0.99, "mrz")
            ex.set("series_number", f"{m.series} {m.number}", 0.99, "mrz")
            ex.set("issue_date", m.issue_date.strftime("%d.%m.%Y") if m.issue_date else None, 0.99, "mrz")
            ex.set("department_code", m.department_code, 0.99, "mrz")
        else:
            ex.notes.append("MRZ найдена, но контрольные цифры не сошлись: " +
                            ", ".join(k for k, ok in m.checks.items() if not ok))
    else:
        ex.notes.append("MRZ не найдена (у паспортов, выданных примерно до 2011 г., её нет).")

    # 4. VLM для недостающих/сомнительных полей
    wanted = ["surname", "given_name", "patronymic", "birth_date", "issue_date",
              "department_code", "series_number", "issued_by", "birth_place", "sex"]
    weak = [k for k in wanted if k not in ex.fields or ex.fields[k].confidence < 0.6]
    if vlm and weak:
        try:
            log(f"Паспорт: дочитываю моделью слабые поля: {', '.join(weak)}…")
            data = vlm_extract(img, vlm)
            for k in weak:
                v = data.get(k)
                if not v:
                    continue
                v = str(v)
                if k == "series_number":
                    v = parsers.normalize_passport(v)
                elif k in ("issue_date", "birth_date"):
                    v = parsers.fmt(parsers.parse_date(v))
                elif k == "department_code":
                    m = DEPT_RE.search(v.replace(".", "-"))
                    v = f"{m.group(1)}-{m.group(2)}" if m else None
                elif k == "sex":
                    v = {"М": "МУЖ", "Ж": "ЖЕН"}.get(v.strip().upper()[:1])
                else:
                    v = v.upper()
                if v:
                    old = ex.fields.get(k)
                    ex.set(k, v, 0.55, "vlm")
                    if old is not None and old.value != v:
                        ex.fields[k].alternatives = [old.value]
        except Exception as e:  # noqa: BLE001
            ex.notes.append(f"VLM недоступна: {e}")

    ex.notes += plausibility(ex)
    if sum(1 for k in ("surname", "given_name", "patronymic", "birth_date") if ex.fields.get(k) and
           ex.fields[k].source in ("ocr", "mrz")) == 0:
        ex.notes.append("Основные поля паспорта не прочитаны OCR — возможно, паспорт заполнен от руки. "
                        "Значения от модели нужно подтвердить вручную.")
    for k in wanted:
        f = ex.fields.get(k)
        if f is None:
            ex.notes.append(f"Поле «{k}» не распознано.")
        elif f.confidence < 0.6 or f.source == "vlm":
            f.needs_review = True
    ex.debug["main_image"] = img
    return ex


# ------------------------------------------------------------------ ранее выданные паспорта

PREV_KEYWORDS = ["СВЕДЕНИЯ", "РАНЕЕ", "ВЫДАННЫХ", "ПАСПОРТАХ", "СЕРИЯ", "НОМЕР"]
PREV_ROW_RE = re.compile(r"(?<!\d)(\d{4})\s+(\d{6})(?!\d)\s*(\d{3}\s?[-—–]\s?\d{3})?\s*(\d{2}[.,]\d{2}[.,]\d{4})?")


def find_previous_passports(pages: list[Page], skip_index: int | None = None) -> list[dict]:
    """Ищет страницу «Сведения о ранее выданных паспортах» и читает с неё серии/номера."""
    found: dict[str, dict] = {}
    for p in pages:
        if p.image is None or p.index == skip_index:
            continue
        log(f"Паспорт: ищу «ранее выданные паспорта» на стр. {p.index + 1}…")
        angle, score = best_orientation(p.image, PREV_KEYWORDS)
        if score < 2:
            continue
        img = rotate(p.image, angle)
        for v in (flatten(img[:, :, 2]), cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)):
            txt, _ = tesseract_text(v, psm=6)
            for ser, num, dept, dt in PREV_ROW_RE.findall(txt):
                if ser.startswith(("19", "20")) and not dept:
                    continue  # это год, а не серия
                key = f"{ser} {num}"
                rec = found.setdefault(key, {"series_number": key, "department_code": "", "issue_date": "",
                                             "page": p.index + 1})
                if dept and not rec["department_code"]:
                    rec["department_code"] = re.sub(r"\s", "", dept).replace("—", "-").replace("–", "-")
                d = parsers.parse_date(dt.replace(",", ".")) if dt else None
                if d and not rec["issue_date"]:
                    rec["issue_date"] = parsers.fmt(d)
    return list(found.values())
