"""Извлечение полей из заявлений по YAML-конфигам форм.

Два режима:
- документ с текстовым слоем (PDF из Word, DOCX) — регулярные выражения из конфига;
- скан/фото — поиск печатных подписей полей (якорей) и распознавание области рядом с ними:
  Tesseract для печатного текста, локальная VLM (если включена) для рукописного.
  Вырезанные фрагменты сохраняются, чтобы человек мог быстро проверить их в отчёте.

Бланк целиком описывается в forms/<бланк>.yaml (поля, регулярные выражения, области на скане, роли полей,
печатные подписи бланка). Разбор значения выбирается по `type:` поля из реестра FIELD_PARSERS; новый тип
значения добавляется функцией с @field_type("имя").
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from rapidfuzz import fuzz

from . import formspec, llm_extract, names, parsers
from .formspec import FORMS_DIR, load_forms  # noqa: F401  (load_forms — прежний адрес, им пользуются скрипты)
from .models import Extraction
from .models import FieldValue
from .ocr import (Line, OllamaVLM, Page, downscale, keyword_score, remove_lines, remove_lines_soft, straighten_photo,
                  tesseract_lines, tesseract_text)
from .passport_rf import KEYWORDS as PASSPORT_KW
from .passport_rf import flatten
from .progress import log

# ------------------------------------------------------------------ конфиги

def detect_form(text: str, forms: list[dict]) -> dict | None:
    t = re.sub(r"\s+", " ", text.lower())
    best, best_hits = None, 0
    for f in forms:
        kws = f["detect"]["keywords"]
        hits = sum(1 for k in kws if k.lower() in t or fuzz.partial_ratio(k.lower(), t) > 85)
        if hits >= f["detect"].get("min_hits", 2) and hits > best_hits:
            best, best_hits = f, hits
    return best


# ------------------------------------------------------------------ разбор значений

def _clean(s: str, keep_lines: bool = False) -> str:
    """Убирает подчёркивания и печатные подписи полей бланка («(кем выдан)» и т. п. — `labels_re` в YAML
    бланков). keep_lines=True — переводы строк сохраняются (нужны для списка доменов: по ним видно, где
    имя домена перенесено на следующую строку)."""
    s = re.sub(r"_{2,}", " ", s or "")
    labels = formspec.labels_regex()
    if labels is not None:
        s = labels.sub(" ", s)
    if keep_lines:
        s = re.sub(r"[ \t]+", " ", s)
        return re.sub(r" ?\n[\s]*", "\n", s).strip(" ,;:/|\n")
    return re.sub(r"\s+", " ", s).strip(" ,;:/|")


# Разбор значения поля по его type: функция (s — очищенное значение в одну строку, raw — как прочитано)
# → {подполе: значение}, ключ "" — основное значение. Новый тип: функция с @field_type("имя").
FIELD_PARSERS: dict[str, Callable[[str, str], dict]] = {}


def field_type(name: str):
    def deco(fn):
        FIELD_PARSERS[name] = fn
        return fn
    return deco


@field_type("text")
def _p_text(s: str, raw: str) -> dict:
    return {"": s}


FIELD_PARSERS["services"] = _p_text
FIELD_PARSERS["org"] = _p_text


# В шапке после ФИО и даты рождения обычно идут паспортные данные — дату рождения ищем до них
_HEADER_STOP = re.compile(r"паспорт|серия|(?<![\d+])\d{2}\s?\d{2}\s?[№N]?\s?\d{6}(?!\d)|\bвыдан|\bИНН\b|"
                          r"зарегистр|адрес|\(ФИО|место\s+рожд", re.I)


@field_type("fio_date")
def _p_fio_date(s: str, raw: str) -> dict:
    """«от Шейко Юрия Борисовича, 27 апреля 1988 года рождения, паспорт …» → ФИО (им. падеж) и дата рождения."""
    stop = _HEADER_STOP.search(s)
    head = s[:stop.start()] if stop else s
    d = parsers.parse_date(head)
    toks = names.extract_fio(parsers.DATE_WORDS_RE.sub(" ", parsers.DATE_RE.sub(" ", head)))
    return {"": names.to_nominative(" ".join(toks)), "raw_fio": " ".join(toks), "birth_date": parsers.fmt(d)}


@field_type("fio")
def _p_fio(s: str, raw: str) -> dict:
    """«Шейко Юрий Борисович, прошу передать права…» → «Шейко Юрий Борисович»; «Виктор Дмитриевич Андреев» →
    «Андреев Виктор Дмитриевич» (порядок Ф И О)."""
    return {"": " ".join(names.ordered(names.extract_fio(s)))}


@field_type("inn")
def _p_inn(s: str, raw: str) -> dict:
    m = parsers.INN_RE.search(s.replace(" ", ""))
    return {"": m.group(1)} if m else {"raw": s}


_ISSUE_STOP = re.compile(r"зарегистр|адрес|прошу|\bЗаявлени|\(серия|ИНН\b", re.I)


@field_type("passport")
def _p_passport(s: str, raw: str) -> dict:
    """Серия и номер + дата выдачи: после «Дата выдачи:» или первая дата после номера (цифрами или
    «10 апреля 2019 года»)."""
    sn = parsers.normalize_passport(s)
    rest = s[parsers.PASSPORT_RE.search(s).end():] if sn else s
    stop = _ISSUE_STOP.search(rest)
    rest = rest[:stop.start()] if stop else rest
    m = re.search(r"дата\s+выдачи\s*:?\s*(.{0,40})", rest, re.I)
    d = parsers.parse_date(m.group(1)) if m else None
    return {"": sn, "issue_date": parsers.fmt(d or parsers.parse_date(rest))}


@field_type("issuer")
def _p_issuer(s: str, raw: str) -> dict:
    s = re.sub(r"(?:паспорт\s+)?\bвыдан[а-я]*\b\s*:?", " ", s, flags=re.I)
    s = re.sub(r"\bкод\s+подразделения\s*:?\s*\d{3}\s?[-—–]\s?\d{3}", " ", s, flags=re.I)
    s = re.sub(r"\bдата\s+выдачи\s*:?", " ", s, flags=re.I)
    yr = r"(?:\s*(?:года|г\.?)(?=[\s,.;]|$))?"          # «… 03 июня 2009 года», «… 25.05.2010 г.»
    s = re.sub(parsers.DATE_WORDS_RE.pattern + yr, " ", s, flags=re.I)
    s = re.sub(parsers.DATE_RE.pattern + yr, " ", s)
    return {"": re.sub(r"\s+", " ", s).strip(" ,.;")}


@field_type("domains")
def _p_domains(s: str, raw: str) -> dict:
    # переводы строк сохраняем: «business-⏎dvd.ru» — одно имя, а не «business-» и «dvd.ru»
    dl = parsers.parse_domain_list(_clean(raw, keep_lines=True))
    doms = dl.domains
    return {"": ", ".join(doms), "list": doms, "punycode": [parsers.to_punycode(d) for d in doms],
            "joined": dl.joined, "fragments": dl.fragments, "mixed": dl.mixed,
            "stated_count": dl.stated_count}


ORG_RE = re.compile(r"\b(?:ООО|АО|ПАО|ЗАО|ОАО|НКО|АНО|ФГУП|МУП|ГУП|ТОО|LLC|Ltd|LIMITED|Inc)\b|"
                    r"Общество\s+с\s+ограниченной|Акционерное\s+общество|Некоммерческ\w+\s+организац", re.I)


@field_type("fio_or_org")
def _p_fio_or_org(s: str, raw: str) -> dict:
    if ORG_RE.search(s):
        return {"": s, "kind": "org"}
    ip = bool(re.match(r"ИП\b|Индивидуальн\w+\s+предпринимател", s, re.I))
    fio = " ".join(names.ordered(names.extract_fio(re.sub(r"^(?:ИП|Индивидуальн\w+\s+предпринимател\w*)\b", "", s,
                                                          flags=re.I))))
    return {"": names.to_nominative(fio), "kind": "ip" if ip else "person", "raw_fio": fio}


@field_type("contact")
def _p_contact(s: str, raw: str) -> dict:
    emails = parsers.EMAIL_RE.findall(s)
    phones = [re.sub(r"[^\d+]", "", p) for p in parsers.PHONE_RE.findall(s)]
    return {"": s, "emails": emails, "phones": phones}


@field_type("date_words")
def _p_date_words(s: str, raw: str) -> dict:
    return {"": parsers.fmt(parsers.parse_date_words(s)), "raw": s}


def parse_value(ftype: str, raw: str) -> dict:
    """Возвращает словарь {подполе: значение}. Ключ "" — основное значение."""
    s = _clean(raw)
    if not s:
        return {}
    return FIELD_PARSERS.get(ftype, _p_text)(s, raw)


def _store(ex: Extraction, key: str, ftype: str, raw: str, conf: float, source: str, crop: str | None = None):
    parsed = parse_value(ftype, raw)
    main = parsed.pop("", None)
    if main in (None, "", []) and crop is None:
        return
    if ftype == "domains":
        # список доменов заменяется целиком: старые «обрывки»/«склеено» от прошлого прочтения не должны остаться.
        # «Всего N доменов» из текста заявления сохраняем, если в новом значении (правка оператора) его нет.
        old_count = ex.get(f"{key}.stated_count")
        for k in [k for k in ex.fields if k.startswith(key + ".")]:
            del ex.fields[k]
        if parsed.get("stated_count") is None and old_count is not None:
            parsed["stated_count"] = old_count
    needs_review = conf < 0.75 or main in (None, "")
    from .models import FieldValue
    ex.fields[key] = FieldValue(main or "", round(conf, 2), source, raw.strip(), needs_review, crop)
    for sub, v in parsed.items():
        if v not in (None, "", []):
            ex.set(f"{key}.{sub}", v, conf, source, raw=raw.strip(), needs_review=needs_review)


# ------------------------------------------------------------------ раздел «новому Администратору»

def _block_text(text: str, cfg: dict) -> str | None:
    """Текст раздела от `start` до первого из `end` (не дальше max_lines строк)."""
    m = None
    for pat in cfg.get("start") or []:
        m = re.search(pat, text)
        if m:
            break
    if m is None:
        return None
    rest = text[m.end():]
    ends = [e.start() for pat in cfg.get("end") or [] for e in [re.search(pat, rest)] if e]
    if ends:
        rest = rest[:min(ends)]
    return "\n".join(rest.splitlines()[:int(cfg.get("max_lines", 14))])


_CUT_ORG = re.compile(r"[,.;]?\s*(?:\bИНН\b|\bОГРН|\bКПП\b|юридический\s+адрес|\bАдрес\b).*$", re.I)
_CELL_LABELS = re.compile(r"(?i)\bНаименование(?:\s+юр\.?\s*лица)?|\bюр\.?\s*лица|\bФИО\b|[|]")


def _person_line(line: str) -> str | None:
    """Строка похожа на ФИО (2–4 слова с заглавной, хотя бы одно — словарное имя или отчество)?"""
    if "@" in line or ORG_RE.search(line):
        return None
    toks = names.extract_fio(_CELL_LABELS.sub(" ", line))
    if len(toks) < 2:
        return None
    if not any(names.is_patronymic(t) or names.is_known(t, "Name") for t in toks):
        return None
    return line


def parse_new_admin_block(block: str) -> dict[str, str]:
    """Раздел «новому Администратору» целиком → {org, person, org_contact, contact} (сырые строки).
    Работает и для таблицы бланка, у которой OCR потерял подписи («Для физ.лиц и ИП:», «ФИО»), и для
    заявлений в свободной форме («новому Администратору: ООО “Ветстем”, ОГРН …, +7 (495) …»)."""
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    org_i = person_i = None
    out: dict[str, str] = {}
    for i, l in enumerate(lines):
        if org_i is None and ORG_RE.search(l):
            v = _CUT_ORG.sub("", _CELL_LABELS.sub(" ", l)).strip(" ,.;:")
            if len(v) >= 4:
                org_i, out["org"] = i, re.sub(r"\s+", " ", v)
                continue
        if person_i is None and _person_line(l):
            v = _CUT_ORG.sub("", _CELL_LABELS.sub(" ", l)).strip(" ,.;:")
            person_i, out["person"] = i, re.sub(r"\s+", " ", v)
    for i, l in enumerate(lines):
        if not (parsers.EMAIL_RE.search(l) or parsers.PHONE_RE.search(l)):
            continue
        key = "contact" if (person_i is not None and i > person_i) or org_i is None else "org_contact"
        if person_i is not None and org_i is not None and person_i < org_i:
            key = "org_contact" if i > org_i else "contact"
        v = re.sub(r"(?i)^[^@\d+]*?(?=[\w.+\-]+@|\+?\d)", "", l, count=1)   # подпись строки («e-mail/тел.»)
        out[key] = (out[key] + "; " if key in out else "") + v.strip(" |")
    return out


_BLOCK_FIELDS = {"org": "new_admin_org", "person": "new_admin_fio", "org_contact": "new_admin_org_contact",
                 "contact": "new_admin_contact"}


def _usable(key: str, value) -> bool:
    """Значение поля нового администратора правдоподобно (иначе это обрывок подписей бланка)."""
    v = value if isinstance(value, str) else ""
    if not v:
        return False
    if key == "new_admin_org":
        return bool(ORG_RE.search(v))
    if key == "new_admin_fio":
        return len(names.extract_fio(re.sub(r"^ИП\b", "", v))) >= 2 or bool(ORG_RE.search(v))
    if key in ("new_admin_contact", "new_admin_org_contact"):
        return bool(parsers.EMAIL_RE.search(v) or parsers.PHONE_RE.search(v))
    return True


def apply_new_admin_block(ex: Extraction, text: str, form: dict, conf: float, source: str) -> bool:
    """Разбор раздела «новому Администратору» (ключ `new_admin_block` бланка): неправдоподобные значения
    полей нового администратора убираются, пустые заполняются из раздела. True — раздел что-то дал."""
    cfg = form.get("new_admin_block")
    if not cfg:
        return False
    block = _block_text(text, cfg)
    found = parse_new_admin_block(block) if block else {}
    roles = cfg.get("fields") or _BLOCK_FIELDS
    for part, key in roles.items():
        if key not in form["fields"]:
            continue
        cur = ex.fields.get(key)
        if cur is not None and cur.source == "manual":
            continue
        if cur is not None and not _usable(key, cur.value):
            for k in [k for k in ex.fields if k == key or k.startswith(key + ".")]:
                del ex.fields[k]
            cur = None
        if part in found and (cur is None or not cur.value):
            for k in [k for k in ex.fields if k == key or k.startswith(key + ".")]:
                del ex.fields[k]
            _store(ex, key, form["fields"][key]["type"], found[part], conf, source)
    # контакты «юрлица», когда юрлица нет, — это контакты нового администратора-физлица
    org_c, org, fio_c = (ex.fields.get(roles.get(k, "")) for k in ("org_contact", "org", "contact"))
    if org_c is not None and org_c.source != "manual" and not (org is not None and org.value):
        okey, ckey = roles.get("org_contact"), roles.get("contact")
        if ckey in form["fields"] and not (fio_c is not None and fio_c.value):
            _store(ex, ckey, form["fields"][ckey]["type"], org_c.raw or str(org_c.value), org_c.confidence,
                   org_c.source)
        for k in [k for k in ex.fields if k == okey or k.startswith(okey + ".")]:
            del ex.fields[k]
    return bool(set(found) & {"org", "person"})


# ------------------------------------------------------------------ текстовый слой

def extract_from_text(text: str, form: dict, source_file: str, conf: float = 0.98, source: str = "text") -> Extraction:
    ex = Extraction(doc_type=form["id"], source_file=source_file)
    ex.debug["mode"] = "text-layer"
    for key, spec in form["fields"].items():
        for pat in spec.get("text", []):
            m = re.search(pat, text)
            if m and _clean(m.group(1)):
                _store(ex, key, spec["type"], m.group(1), conf, source)
                if key in ex.fields and ex.fields[key].value:
                    break
    apply_new_admin_block(ex, text, form, conf, source)
    return ex


def text_fields(text: str, form: dict, source_file: str, conf: float = 0.98, source: str = "text",
                only: list[str] | None = None) -> Extraction:
    """Поля из текста заявления: регулярками бланка или — в экспериментальном режиме (DOCTOOL_LLM_FIELDS) —
    локальной моделью (llm_extract). Модель недоступна → регулярки, с пометкой в замечаниях."""
    if llm_extract.enabled() and len(re.sub(r"\W", "", text or "")) >= 40:
        try:
            return llm_extract.extract(text, form, source_file, conf=conf, source=source, only=only)
        except llm_extract.LLMError as e:
            log(f"   модель не ответила: {e} — читаю поля регулярками бланка")
            ex = extract_from_text(text, form, source_file, conf, source)
            ex.notes.append(f"Модель для полей заявления не ответила ({e}) — поля прочитаны регулярками бланка.")
            ex.debug["llm_error"] = str(e)
            return ex
    return extract_from_text(text, form, source_file, conf, source)


def _keep_src(f: FieldValue, default: str = "ocr-text") -> str:
    """Источник значения из текста страницы: «llm-…» сохраняем (видно, что поле читала модель)."""
    return f.source if f.source.startswith("llm") else default


# ------------------------------------------------------------------ скан

@dataclass
class Anchor:
    text: str
    box: tuple[int, int, int, int]
    score: float
    h: int = 0          # высота строки по медиане слов (бокс строки бывает раздут рукописью)


def _find_anchor(lines: list[Line], variants: list[str], min_y: int = 0) -> Anchor | None:
    """Ищет подпись поля (нечётко). Поля в конфиге идут в порядке бланка, поэтому
    подпись ищется не выше предыдущей найденной (min_y)."""
    best = None
    for l in lines:
        if l.box[3] < min_y:
            continue
        ws = l.words
        for i in range(len(ws)):
            for j in range(i + 1, min(len(ws), i + 9) + 1):
                cand = " ".join(w.text for w in ws[i:j])
                for v in variants:
                    sc = fuzz.ratio(cand.lower(), v.lower())
                    need = 85 if len(v) <= 8 else 72   # короткие подписи — почти точное совпадение
                    if sc >= need and (best is None or sc > best.score):
                        box = (min(w.x for w in ws[i:j]), min(w.y for w in ws[i:j]),
                               max(w.x + w.w for w in ws[i:j]), max(w.y + w.h for w in ws[i:j]))
                        wh = int(np.median([w.h for w in ws[i:j]]))
                        best = Anchor(cand, box, sc, wh)
    return best


def _label_lines(img: np.ndarray) -> list[Line]:
    """Несколько проходов OCR по печатному тексту бланка. Синий канал «стирает» синюю ручку."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    out = []
    for v, psm in ((gray, 3), (flatten(img[:, :, 0]), 3), (gray, 11)):
        out += tesseract_lines(v, lang="rus+eng", psm=psm)
    return out


def crop_for_vlm(crop: np.ndarray) -> np.ndarray:
    """Для модели: убираем подчёркивания (сохраняя цвет чернил)."""
    clean = remove_lines(crop)
    mask = clean == 255
    out = crop.copy()
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    out[(mask) & (g < 255)] = 255
    return out


def _ink_ratio(crop: np.ndarray) -> tuple[float, float]:
    """Доля «синих» и вообще тёмных пикселей — признак рукописного заполнения."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, (95, 60, 40), (135, 255, 255))
    dark = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) < 140
    n = crop.shape[0] * crop.shape[1]
    return float(blue.sum() / 255 / n), float(dark.sum() / n)


def extract_from_scan(page: Page, form: dict, out_dir: Path, vlm: OllamaVLM | None = None,
                      handwritten_mode: bool | None = None, prefer: Extraction | None = None) -> Extraction:
    """handwritten_mode: True — все заполненные поля считаем рукописными (VLM, если есть);
    False — рукописи нет, только Tesseract; None — определяем по каждому полю (синие чернила / уверенность)."""
    img = page.image
    H, W = img.shape[:2]
    ex = Extraction(doc_type=form["id"], source_file=page.source)
    ex.debug["mode"] = "scan"
    log("Заявление (скан): ищу подписи полей на бланке…")
    lines = _label_lines(img)
    # «печатные строки бланка» — только подписи полей и постоянный текст формы. Значения, напечатанные
    # на компьютере, сюда попадать не должны, иначе область значения обрежется (так было на заявлении Аверина).
    form_labels = [a for spec in form["fields"].values() for a in (spec.get("scan") or {}).get("anchor", [])]
    form_labels += form.get("static_text", []) + form["detect"]["keywords"]
    label_words = {w.lower() for lab in form_labels for w in re.findall(r"[А-Яа-яЁёA-Za-z]{2,}", lab)}
    printed = [l for l in lines if l.conf >= 60 and len(l.text) >= 6
               and any(fuzz.partial_ratio(lab.lower(), l.text.lower()) >= 85 and len(l.text) <= len(lab) * 1.6 + 10
                       for lab in form_labels)]
    overlay_boxes: list = []
    min_y = 0
    crops_dir = out_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    handwritten_fields = 0

    scan_fields = [k for k, sp in form["fields"].items() if sp.get("scan")]
    prefixes = formspec.prefix_regex(form)
    for key, spec in form["fields"].items():
        sc = spec.get("scan")
        if not sc:
            continue
        log(f"Заявление: поле {scan_fields.index(key) + 1}/{len(scan_fields)} «{spec['title']}»")
        a = _find_anchor(lines, sc["anchor"], min_y)
        if not a:
            ex.notes.append(f"Не найдена подпись поля «{spec['title']}» на скане.")
            continue
        x0, y0, x1, y1 = a.box
        min_y = y0 - 5
        h = max(20, a.h or (y1 - y0))
        r = sc["region"]
        ry0 = int(max(0, y0 + r["dy0"] * h)); ry1 = int(min(H, y0 + r["dy1"] * h))
        rx0 = int(r["x0"] * W); rx1 = int(min(W, r["x1"] * W))
        if r["dy0"] < 0 and sc.get("clip", True):
            # не залезаем на печатную строку бланка выше (подпись предыдущего поля)
            for pl in printed:
                bx0, by0, bx1, by1 = pl.box
                if by0 < y0 - 0.8 * h and by1 > ry0 and min(bx1, rx1) - max(bx0, rx0) > 0.3 * (bx1 - bx0):
                    ry0 = max(ry0, (by0 + by1) // 2)  # рукопись часто наезжает на строку выше
        overlay_boxes.append((key, (rx0, ry0, rx1, ry1), a.box))
        if ry1 - ry0 < 10 or rx1 - rx0 < 10:
            continue
        crop = img[ry0:ry1, rx0:rx1]
        crop_path = crops_dir / f"{Path(page.source).stem}_{key}.png"
        cv2.imencode(".png", crop)[1].tofile(str(crop_path))

        blue, dark = _ink_ratio(crop)
        if blue < 0.002 and dark < 0.01:
            ex.set(key, "", 0.9, "scan", raw="(пусто)", crop=str(crop_path))
            continue
        # печатный текст: Tesseract (без подчёркиваний — они сильно портят распознавание)
        txt, conf = tesseract_text(remove_lines(crop), lang="rus+eng", psm=6)
        txt = txt.replace("\n", " ")
        pv = prefer.fields.get(key) if prefer is not None else None
        # пустое поле: чернил нет (после удаления линий) или, кроме слов бланка, ничего не распознано
        rest = [w for w in re.findall(r"[\wА-Яа-яЁё@.]{2,}", txt)
                if not any(fuzz.ratio(w.lower(), lw) >= 75 for lw in label_words)]
        dark_clean = float((remove_lines(crop) < 140).mean())
        no_page_value = not (pv is not None and pv.value)
        if blue <= 0.002 and no_page_value and (dark_clean < 0.004 or len("".join(rest)) < 4
                                                 or (conf < 45 and dark_clean < 0.03)):
            ex.fields[key] = FieldValue("", 0.8, "scan", "(пусто)", False, str(crop_path))
            continue
        is_printed = blue <= 0.004 and (conf >= 75 or (pv is not None and pv.value))
        if handwritten_mode is None:
            handwritten = not is_printed and (blue > 0.004 or conf < 70)
        else:
            # флаг «рукописное»: печатные поля (без синих чернил и с уверенным OCR) всё равно читаем OCR
            handwritten = handwritten_mode and not (blue <= 0.004 and conf >= 75 and pv is not None and pv.value)
        if not handwritten and pv is not None and pv.value and conf >= 70 and _region_extends(pv.raw or str(pv.value), txt):
            pv = None          # в тексте страницы значение оборвано посреди слова, а в области прочитано целиком
        if not handwritten and pv is not None and pv.value:
            # значение из распознанного текста всей страницы (там строки целиком, без обрезки по областям)
            for k, f in prefer.fields.items():
                if k == key or k.startswith(key + "."):
                    ex.fields[k] = FieldValue(f.value, f.confidence, _keep_src(f), f.raw, f.needs_review,
                                              str(crop_path) if k == key else None)
            continue
        if handwritten and vlm is None and pv is not None and pv.value:
            # «рукопись» (или печатный текст под штампом/подписью), модели нет: берём прочтение всей страницы,
            # но как ненадёжное — проверки дадут «ручную проверку», а не «расхождение»
            handwritten_fields += 1
            for k, f in prefer.fields.items():
                if k == key or k.startswith(key + "."):
                    ex.fields[k] = FieldValue(f.value, min(f.confidence, 0.55), _keep_src(f), f.raw, True,
                                              str(crop_path) if k == key else None)
            continue
        if not handwritten and _form_text_only(txt, label_words):
            # в области — только печатный текст бланка (подпись поля нашлась не там): значения нет
            ex.fields[key] = FieldValue("", 0.6, "scan", "(пусто)", True, str(crop_path))
            continue
        source, value, c = "ocr", txt, conf / 100
        if handwritten:
            handwritten_fields += 1
            if vlm:
                try:
                    log("   рукопись → локальная модель…")
                    value, source, c = vlm.transcribe(crop_for_vlm(crop), spec["title"]), "vlm", 0.6
                    log(f"   прочитано: {value or '(пусто)'}")
                except Exception as e:  # noqa: BLE001
                    ex.notes.append(f"VLM, поле «{spec['title']}»: {e}")
                    value, c = "", 0.0
            else:
                value, c = "", 0.0   # без VLM рукопись не распознаём — только фрагмент для проверки
        # убираем печатные слова-префиксы («от», «Я,», «профиля):» — `strip_prefixes` бланка)
        value = value.strip()
        if prefixes is not None:
            value = prefixes.sub("", value)
        value = value.lstrip("):;,. ")
        _store(ex, key, spec["type"], value, c, source, crop=str(crop_path))
        if key in ex.fields and handwritten:
            ex.fields[key].needs_review = True
            ex.fields[key].source += "+рукопись"
    ov = img.copy()
    for key, (rx0, ry0, rx1, ry1), ab in overlay_boxes:
        cv2.rectangle(ov, (rx0, ry0), (rx1, ry1), (0, 160, 0), 3)
        cv2.rectangle(ov, ab[:2], ab[2:], (0, 0, 255), 2)
        cv2.putText(ov, key, (rx0 + 5, ry0 + 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 120, 0), 2)
    ov_path = crops_dir / f"{Path(page.source).stem}__regions.jpg"
    cv2.imencode(".jpg", ov)[1].tofile(str(ov_path))
    ex.debug["regions_overlay"] = str(ov_path)
    if handwritten_fields:
        msg = f"Рукописных полей: {handwritten_fields}."
        msg += (" Распознаны локальной VLM — сверьте с фрагментами." if vlm else
                " VLM не включена: значения нужно подтвердить по фрагментам в отчёте.")
        ex.notes.append(msg)
    return ex


def _region_extends(page_value: str, region_txt: str) -> bool:
    """Значение из текста страницы оборвано посреди слова, а OCR области читает его же дальше:
    «Виктор Дмитриевич Ан» (страница) и «Виктор Дмитриевич Андреев» (область)."""
    norm = lambda x: re.sub(r"\s+", " ", re.sub(r"_{2,}", " ", x or "")).strip().lower().replace("ё", "е")  # noqa: E731
    a, b = norm(page_value), norm(region_txt)
    if len(a) < 4:
        return False
    i = b.find(a)
    return i >= 0 and i + len(a) < len(b) and b[i + len(a)].isalpha() and a[-1].isalpha()


def _form_text_only(txt: str, label_words: set[str]) -> bool:
    """Распознанный текст области состоит в основном из слов бланка («и предоставляемых по этим доменам…»)."""
    words = [w.lower() for w in re.findall(r"[А-Яа-яЁёA-Za-z]{3,}", txt or "")]
    if len(words) < 3:
        return False
    known = sum(1 for w in words if w in label_words or any(fuzz.ratio(w, lw) >= 85 for lw in label_words))
    return known / len(words) >= 0.6


# ------------------------------------------------------------------ длинный список доменов на скане

def _find_line(lines: list[Line], variants: list[str], below: int = -1) -> Line | None:
    """Первая сверху строка OCR, похожая на одну из подписей бланка (ниже y=below)."""
    for l in lines:
        if l.box[1] <= below:
            continue
        t = l.text.lower()
        if any(fuzz.partial_ratio(v.lower(), t) >= 85 for v in variants if len(t) >= len(v) * 0.6):
            return l
    return None


def _domain_issues(dl: parsers.DomainList) -> bool:
    return not dl.domains or dl.count_mismatch or bool(dl.fragments) or bool(dl.mixed)


def _reread_domain_list(page: Page, lines: list[Line], form: dict, prefer: Extraction, out_dir: Path) -> dict | None:
    """Многострочный список доменов на скане перечитывается отдельно — абзацем.

    OCR всей страницы (psm 4) читает длинный подчёркнутый абзац хуже: линия режет буквы с хвостами вниз
    («.рф» пропадает, «р» читается как «о»). Абзац от «…администрированию домена(ов)» до подписи
    «(наименование домена(ов))» распознаётся ещё трижды: как есть (psm 6) и с мягким удалением подчёркиваний
    (psm 6 и 4). Из четырёх прочтений берётся то, которое лучше сходится: число доменов = «всего N»
    из заявления, нет обрывков без зоны, домены подтверждены большинством прочтений. Домены, которые
    большинство не подтвердило, попадают в «прочитаны по-разному» (ручная проверка)."""
    key = next((k for k, sp in form["fields"].items() if sp.get("type") == "domains"), None)
    if key is None:
        return None
    spec = form["fields"][key]
    sc = spec.get("scan") or {}
    starts = sc.get("block_start") or []
    cur = prefer.fields.get(key)
    cur_raw = cur.raw if cur is not None else ""
    cur_dl = parsers.parse_domain_list(_clean(cur_raw, keep_lines=True))
    multiline = sum(1 for ln in cur_raw.splitlines() if parsers.DOMAIN_RE.search(ln)) >= 2
    if not starts or not (multiline or _domain_issues(cur_dl)):
        return None
    start = _find_line(lines, starts)
    end = _find_line(lines, sc.get("anchor") or [], below=start.box[1] if start else -1)
    if start is None or end is None or end.box[1] <= start.box[3]:
        return None
    H = page.image.shape[0]
    pad = max(4, (start.box[3] - start.box[1]) // 3)
    band = page.image[max(0, start.box[1] - pad):min(H, end.box[3] + pad)]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    soft = remove_lines_soft(gray)
    log("Заявление: перечитываю список доменов отдельным абзацем…")
    readings = [("страница", cur_raw, cur_dl)] if cur_raw else []
    for name, img, psm in (("абзац", gray, 6), ("абзац без линий", soft, 6), ("абзац без линий psm4", soft, 4)):
        txt = "\n".join(l.text for l in tesseract_lines(img, lang="rus+eng", psm=psm))
        for pat in spec.get("text", []):
            m = re.search(pat, txt)
            if m and _clean(m.group(1)):
                readings.append((name, m.group(1), parsers.parse_domain_list(_clean(m.group(1), True))))
                break
        # два прочтения дали один и тот же список без замечаний — дальше не распознаём (экономия времени)
        good = [dl.domains for _, _, dl in readings if dl.domains and not _domain_issues(dl)]
        if any(good.count(x) >= 2 for x in good):
            break
    if not readings:
        return None
    votes = Counter(d for _, _, dl in readings for d in set(dl.domains))
    majority = len(readings) // 2 + 1

    def score(r):
        dl = r[2]
        count_ok = dl.stated_count is not None and not dl.count_mismatch
        return (count_ok, -len(dl.fragments), sum(1 for d in dl.domains if votes[d] >= majority), -len(dl.mixed),
                len(dl.domains))

    best = max(readings, key=score)         # при равенстве остаётся прочтение страницы (первое)
    uncertain = [d for d in best[2].domains if votes[d] < majority] if len(readings) >= 2 else []
    info = {"выбрано": best[0],
            "прочтения": {name: {"доменов": len(dl.domains), "обрывки": dl.fragments,
                                 "указано в заявлении": dl.stated_count} for name, _, dl in readings}}
    crops_dir = out_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    crop_path = crops_dir / f"{Path(page.source).stem}_{key}_list.png"
    cv2.imencode(".png", band)[1].tofile(str(crop_path))
    if best[0] != "страница" or uncertain:
        for k in [k for k in prefer.fields if k == key or k.startswith(key + ".")]:
            del prefer.fields[k]
        _store(prefer, key, "domains", best[1], 0.85, "ocr-text")
        if uncertain:
            prefer.set(f"{key}.uncertain", uncertain, 0.85, "ocr-text")
    note = None
    if best[0] != "страница" and readings[0][0] == "страница":
        was, now = len(cur_dl.domains), len(best[2].domains)
        note = (f"Список доменов перечитан отдельным абзацем: распознано {now} (по всей странице было {was})"
                + (f", в заявлении указано {best[2].stated_count}" if best[2].stated_count is not None else "") + ".")
    return {"key": key, "crop": str(crop_path), "info": info, "note": note}


# ------------------------------------------------------------------ страницы заявления в файле

def page_kind(text: str, form: dict) -> str | None:
    """Страница заявления: "start" — первая страница бланка (заголовок, ключевые слова), "cont" — продолжение
    (раздел «новому Администратору», нумерованный список доменов, подписи), None — другое (конверт, опись,
    нотариальная надпись, паспорт)."""
    t = re.sub(r"\s+", " ", (text or "").lower())
    det = form.get("detect") or {}
    if any(fuzz.partial_ratio(x.lower(), t) >= 85 for x in det.get("title") or []) or detect_form(text, [form]):
        return "start"
    marks = sum(1 for x in det.get("continuation") or [] if fuzz.partial_ratio(x.lower(), t) >= 85)
    numbered = len(re.findall(r"(?m)^\s*\d{1,3}[).]\s*\S+\.[a-zа-яё]{2,}", text or ""))
    return "cont" if marks >= 2 or numbered >= 3 else None


def _quick_text(page: Page) -> str:
    return tesseract_text(downscale(cv2.cvtColor(page.image, cv2.COLOR_BGR2GRAY), 1500), lang="rus", psm=3)[0]


def _full_lines(page: Page) -> list[Line]:
    return tesseract_lines(cv2.cvtColor(page.image, cv2.COLOR_BGR2GRAY), lang="rus+eng", psm=4)


def _application_groups(pages: list[Page], form: dict, first_text: str) -> tuple[list[list[Page]], list[int]]:
    """Заявления в многостраничном файле: [[первая страница, продолжения…], …] и номера страниц паспорта.
    Первая страница бланка ищется по заголовку — в файле перед ней бывают конверт и опись."""
    kinds, passport = [], []
    for p in pages:
        txt = first_text if p is pages[0] else _quick_text(p)
        k = page_kind(txt, form)
        kinds.append(k)
        if k is None and keyword_score(txt, PASSPORT_KW) >= 4:
            passport.append(p.index + 1)
    groups: list[list[Page]] = []
    for p, k in zip(pages, kinds):
        if k == "start":
            groups.append([p])
        elif k == "cont" and groups:
            groups[-1].append(p)
    return groups, passport


# ------------------------------------------------------------------ точка входа

def extract_application(pages: list[Page], out_dir: Path, vlm: OllamaVLM | None = None,
                        forms: list[dict] | None = None, mode: str = "auto",
                        handwritten: bool | None = None, form_id: str | None = None) -> Extraction:
    """mode: auto | electronic | scan | photo. form_id — принудительно использовать этот бланк.

    Многостраничный файл (скан): страница бланка ищется по заголовку (перед ней бывают конверт, опись),
    к ней добавляются страницы-продолжения (вторая страница списка доменов, раздел «новому Администратору»).
    Если в файле несколько заявлений (одинаковых, с разными доменами), домены объединяются."""
    forms = forms or load_forms()
    if form_id:
        forced = [f for f in forms if f["id"] == form_id]
        if forced:
            forms = forced
    page = pages[0]
    text = "\n".join(p.text_layer for p in pages)
    has_text = len(re.sub(r"\s", "", text)) > 300
    if mode == "electronic" and not has_text:
        log("Заявление: отмечено как электронное, но текстового слоя нет — обрабатываю как скан")
    if has_text and mode in ("auto", "electronic"):
        form = detect_form(text, forms) or (forms[0] if form_id else None)
        if form:
            log("Заявление: есть текстовый слой, читаю поля из текста")
            ex = text_fields(text, form, page.source)
            # текстовый слой есть, но поля пустые — возможно, заполнено от руки поверх PDF
            filled = sum(1 for k, f in ex.fields.items() if "." not in k and f.value)
            if filled >= 4 or page.image is None:
                return ex
    if page.image is None:
        ex = Extraction(doc_type="unknown", source_file=page.source)
        ex.notes.append("Не удалось разобрать текст документа.")
        return ex
    if mode == "photo":
        img, ok = straighten_photo(page.image)
        log("Заявление (фото): перспектива выровнена" if ok else "Заявление (фото): границы листа не найдены, беру как есть")
        page = Page(image=img, text_layer=page.text_layer, source=page.source, index=page.index)
        pages = [page] + list(pages[1:])
    # скан: распознаём страницу целиком — для напечатанных значений это надёжнее, чем по областям
    log("Заявление: распознаю бланк целиком, определяю форму…")
    lines = _full_lines(page)
    page_text = "\n".join(l.text for l in lines)
    form = detect_form(page_text, forms)
    scan_pages = [p for p in pages if p.image is not None]
    groups, passport_pages, notes = [[page]], [], []
    if len(scan_pages) > 1:
        log(f"Заявление: в файле {len(scan_pages)} стр. — ищу страницы заявления…")
        probe = form or (forms[0] if form_id else None) or forms[0]
        groups, passport_pages = _application_groups(scan_pages, probe, page_text)
        if groups and groups[0][0] is not page:
            page = groups[0][0]
            log(f"Заявление: бланк — на стр. {page.index + 1}")
            lines = _full_lines(page)
            page_text = "\n".join(l.text for l in lines)
            form = detect_form(page_text, forms) or probe
        groups = groups or [[page]]
        for p in groups[0][1:]:
            log(f"Заявление: продолжение — стр. {p.index + 1}")
            page_text += "\n" + "\n".join(l.text for l in _full_lines(p))
        if passport_pages:
            notes.append(f"В файле заявления есть страницы паспорта (стр. {', '.join(map(str, passport_pages))}): "
                         "чтобы сверить с паспортом, отметьте «Заявление и паспорт в одном файле».")
    form = form or (forms[0] if form_id else None)
    if not form:
        ex = Extraction(doc_type="unknown", source_file=page.source)
        ex.notes.append("Тип формы не определён — добавьте YAML-конфиг в папку forms/.")
        return ex
    prefer = text_fields(page_text, form, page.source, conf=0.85, source="ocr-text")
    reread = _reread_domain_list(page, lines, form, prefer, out_dir) if len(groups[0]) == 1 else None
    if prefer.debug.get("mode") == "llm":
        _llm_new_admin(page, lines, form, prefer)
    elif not apply_new_admin_block(prefer, page_text, form, 0.85, "ocr-text"):
        _reread_new_admin(page, lines, form, prefer)
    ex = extract_from_scan(page, form, out_dir, vlm, handwritten_mode=handwritten, prefer=prefer)
    # поля без области на скане (например, «новому Администратору: …») — из текста страницы.
    # Множество полей фиксируется до цикла: иначе после «domains» пропускались бы «domains.list» и др.
    present = {k.split(".")[0] for k in ex.fields}
    for k, f in prefer.fields.items():
        if k.split(".")[0] not in present:
            ex.fields[k] = FieldValue(f.value, f.confidence, _keep_src(f), f.raw)
    if reread:
        f = ex.fields.get(reread["key"])
        if f is not None and f.source in ("ocr-text", "llm-ocr-text"):
            f.crop = reread["crop"]          # фрагмент — весь абзац со списком, а не последняя строка
        ex.debug["domains_reread"] = reread["info"]
        if reread["note"]:
            ex.notes.append(reread["note"])
    if len(groups) > 1:
        _merge_more_applications(ex, groups[1:], form)
    ex.notes.extend(n for n in prefer.notes if n not in ex.notes)
    ex.notes.extend(notes)
    for k in ("llm", "llm_error"):
        if k in prefer.debug:
            ex.debug[k] = prefer.debug[k] + ex.debug.get(k, []) if k == "llm" else prefer.debug[k]
    ex.debug["page_text"] = page_text
    ex.debug["application_pages"] = [[p.index + 1 for p in g] for g in groups]
    return ex


def _merge_more_applications(ex: Extraction, groups: list[list[Page]], form: dict) -> None:
    """Ещё одно заявление того же заявителя в файле: его домены добавляются к списку первого."""
    key = next((k for k, sp in form["fields"].items() if sp.get("type") == "domains"), None)
    if key is None:
        return
    first = list(ex.get(f"{key}.list") or [])
    added, parts = [], [len(first)]
    for g in groups:
        log(f"Заявление: ещё одно заявление — стр. {', '.join(str(p.index + 1) for p in g)}")
        txt = "\n".join("\n".join(l.text for l in _full_lines(p)) for p in g)
        other = text_fields(txt, form, g[0].source, conf=0.85, source="ocr-text")
        ex.debug.setdefault("llm", []).extend(other.debug.get("llm", []))
        doms = [d for d in (other.get(f"{key}.list") or []) if d not in first and d not in added]
        parts.append(len(other.get(f"{key}.list") or []))
        added += doms
        for role in ("applicant_header", "new_admin_org", "new_admin_fio"):
            a, b = ex.get(role), other.get(role)
            if a and b and names.norm(a) != names.norm(b):
                ex.notes.append(f"Заявления в файле различаются: «{form['fields'].get(role, {}).get('title', role)}» — "
                                f"«{a}» и «{b}».")
    if not added:
        return
    f = ex.fields.get(key)
    raw = ((f.raw if f is not None else "") + "\n" + "\n".join(added)).strip()
    conf = f.confidence if f is not None else 0.85
    _store(ex, key, "domains", raw if f is not None and f.raw and f.raw != "(пусто)" else ", ".join(first + added),
           conf, "ocr-text", crop=f.crop if f is not None else None)
    ex.notes.append(f"В файле {len(parts)} заявления(-й): домены объединены ({' + '.join(map(str, parts))}, "
                    f"всего {len(ex.get(f'{key}.list') or [])}).")


def _new_admin_band_text(page: Page, lines: list[Line]) -> str | None:
    """Полоса от «новому Администратору» до «Номер договора», распознанная отдельно как блок (psm 6)."""
    start = _find_line(lines, ["новому Администратору"])
    if start is None:
        return None
    end = _find_line(lines, ["Номер договора", "(номер договора", "профиля):", "(подпись)"], below=start.box[3])
    H = page.image.shape[0]
    y1 = end.box[1] if end is not None else min(H, start.box[3] + 12 * (start.box[3] - start.box[1]))
    if y1 - start.box[3] < 10:
        return None
    band = page.image[max(0, start.box[1] - 4):min(H, y1 + 4)]
    log("Заявление: перечитываю раздел «новому Администратору» отдельно…")
    return "\n".join(l.text for l in tesseract_lines(remove_lines_soft(cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)),
                                                      lang="rus+eng", psm=6))


NEW_ADMIN_KEYS = ("new_admin_inline", "new_admin_org", "new_admin_org_contact", "new_admin_fio", "new_admin_contact")


def _llm_new_admin(page: Page, lines: list[Line], form: dict, prefer: Extraction) -> None:
    """Режим модели: нового администратора не нашли в тексте страницы — полоса раздела перечитывается отдельно
    (psm 6), и модель читает только поля нового администратора."""
    keys = [k for k in NEW_ADMIN_KEYS if k in form["fields"]]
    if not keys or any(prefer.get(k) for k in ("new_admin_org", "new_admin_fio") if k in keys):
        return
    txt = _new_admin_band_text(page, lines)
    if not txt:
        return
    try:
        sub = llm_extract.extract("новому Администратору:\n" + txt, form, page.source, conf=0.8, source="ocr-text",
                                  only=keys)
    except llm_extract.LLMError as e:
        prefer.notes.append(f"Раздел «новому Администратору»: модель не ответила ({e}).")
        return
    got = False
    empty = {k for k in keys if not prefer.get(k)}           # до копирования: иначе подполя (.emails …) пропадут
    for k, f in sub.fields.items():
        if k.split(".")[0] in empty:
            prefer.fields[k] = f
            got = got or ("." not in k and bool(f.value))
    prefer.notes.extend(n for n in sub.notes if n not in prefer.notes)
    prefer.debug.setdefault("llm", []).extend(sub.debug.get("llm", []))
    if got:
        prefer.notes.append("Раздел «новому Администратору» перечитан отдельно (на странице целиком его не видно).")


def _reread_new_admin(page: Page, lines: list[Line], form: dict, prefer: Extraction) -> None:
    """Таблица нового администратора, которую OCR всей страницы (psm 4) пропустил: полоса от «новому
    Администратору» до «Номер договора» распознаётся ещё раз как блок (psm 6)."""
    txt = _new_admin_band_text(page, lines)
    if not txt:
        return
    if apply_new_admin_block(prefer, txt, form, 0.8, "ocr-text"):
        prefer.notes.append("Раздел «новому Администратору» перечитан отдельно (на странице целиком его не видно).")
