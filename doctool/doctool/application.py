"""Извлечение полей из заявлений по YAML-конфигам форм.

Два режима:
- документ с текстовым слоем (PDF из Word, DOCX) — регулярные выражения из конфига;
- скан/фото — поиск печатных подписей полей (якорей) и распознавание области рядом с ними:
  Tesseract для печатного текста, локальная VLM (если включена) для рукописного.
  Вырезанные фрагменты сохраняются, чтобы человек мог быстро проверить их в отчёте.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml
from rapidfuzz import fuzz

from . import names, parsers
from .models import Extraction
from .models import FieldValue
from .ocr import Line, OllamaVLM, Page, remove_lines, straighten_photo, tesseract_lines, tesseract_text
from .passport_rf import flatten
from .progress import log

FORMS_DIR = Path(__file__).resolve().parent.parent / "forms"


# ------------------------------------------------------------------ конфиги

def load_forms(forms_dir: Path = FORMS_DIR) -> list[dict]:
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(forms_dir.glob("*.yaml"))]


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

def _clean(s: str) -> str:
    s = re.sub(r"_{2,}", " ", s or "")
    s = re.sub(r"\((?:ФИО|кем выдан|адрес регистрации|серия, номер паспорта, когда выдан|"
               r"ИНН ИП \(при наличии статуса ИП\)|наименование домена\(ов\)?|Primary, Secondary DNS|"
               r"номер договора с компанией РЕГ\.РУ|фамилия, имя, отчество|подпись|дата прописью)[^)]*\)?",
               " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,;:/|")


def parse_value(ftype: str, raw: str) -> dict:
    """Возвращает словарь {подполе: значение}. Ключ "" — основное значение."""
    s = _clean(raw)
    if not s:
        return {}
    if ftype == "fio_date":
        d = parsers.parse_date(s)
        fio_part = parsers.DATE_RE.sub(" ", s)
        fio_part = re.sub(r"[\d.,]+", " ", fio_part)
        return {"": names.to_nominative(fio_part), "raw_fio": " ".join(names.split_fio(fio_part)),
                "birth_date": parsers.fmt(d)}
    if ftype == "fio":
        return {"": " ".join(names.split_fio(s))}
    if ftype == "inn":
        m = parsers.INN_RE.search(s.replace(" ", ""))
        return {"": m.group(1)} if m else {"raw": s}
    if ftype == "passport":
        sn = parsers.normalize_passport(s)
        rest = s[parsers.PASSPORT_RE.search(s).end():] if sn else s
        return {"": sn, "issue_date": parsers.fmt(parsers.parse_date(rest))}
    if ftype == "issuer":
        s = re.sub(r"\bвыдан[а-я]*\b", " ", s, flags=re.I)
        s = parsers.DATE_RE.sub(" ", s)
        return {"": re.sub(r"\s+", " ", s).strip(" ,.;")}
    if ftype == "domains":
        doms = parsers.find_domains(s)
        return {"": ", ".join(doms), "list": doms, "punycode": [parsers.to_punycode(d) for d in doms]}
    if ftype == "services":
        return {"": s}
    if ftype == "fio_or_org":
        if re.search(r"\b(ООО|АО|ПАО|ЗАО|ОАО|НКО|АНО|ФГУП|МУП)\b", s):
            return {"": s, "kind": "org"}
        ip = bool(re.match(r"ИП\b", s))
        fio = " ".join(names.split_fio(re.sub(r"^ИП\b", "", s)))
        return {"": names.to_nominative(fio), "kind": "ip" if ip else "person", "raw_fio": fio}
    if ftype == "org":
        return {"": s}
    if ftype == "contact":
        emails = parsers.EMAIL_RE.findall(s)
        phones = [re.sub(r"[^\d+]", "", p) for p in parsers.PHONE_RE.findall(s)]
        return {"": s, "emails": emails, "phones": phones}
    if ftype == "date_words":
        return {"": parsers.fmt(parsers.parse_date_words(s)), "raw": s}
    return {"": s}


def _store(ex: Extraction, key: str, ftype: str, raw: str, conf: float, source: str, crop: str | None = None):
    parsed = parse_value(ftype, raw)
    main = parsed.pop("", None)
    if main in (None, "", []) and crop is None:
        return
    needs_review = conf < 0.75 or main in (None, "")
    from .models import FieldValue
    ex.fields[key] = FieldValue(main or "", round(conf, 2), source, raw.strip(), needs_review, crop)
    for sub, v in parsed.items():
        if v not in (None, "", []):
            ex.set(f"{key}.{sub}", v, conf, source, raw=raw.strip(), needs_review=needs_review)


# ------------------------------------------------------------------ текстовый слой

def extract_from_text(text: str, form: dict, source_file: str) -> Extraction:
    ex = Extraction(doc_type=form["id"], source_file=source_file)
    ex.debug["mode"] = "text-layer"
    for key, spec in form["fields"].items():
        for pat in spec.get("text", []):
            m = re.search(pat, text)
            if m and _clean(m.group(1)):
                _store(ex, key, spec["type"], m.group(1), 0.98, "text")
                break
    return ex


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
        if not handwritten and pv is not None and pv.value:
            # значение из распознанного текста всей страницы (там строки целиком, без обрезки по областям)
            for k, f in prefer.fields.items():
                if k == key or k.startswith(key + "."):
                    ex.fields[k] = FieldValue(f.value, f.confidence, "ocr-text", f.raw, f.needs_review,
                                              str(crop_path) if k == key else None)
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
        # убираем печатные слова-префиксы («от», «Я,», «профиля):» и т. п.)
        value = re.sub(r"^(от\b|я,|профиля\)\s*:)\s*", "", value.strip(), flags=re.I).lstrip("):;,. ")
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


# ------------------------------------------------------------------ точка входа

def extract_application(pages: list[Page], out_dir: Path, vlm: OllamaVLM | None = None,
                        forms: list[dict] | None = None, mode: str = "auto",
                        handwritten: bool | None = None, form_id: str | None = None) -> Extraction:
    """mode: auto | electronic | scan | photo. form_id — принудительно использовать этот бланк."""
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
            ex = extract_from_text(text, form, page.source)
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
    # скан: распознаём страницу целиком — для напечатанных значений это надёжнее, чем по областям
    log("Заявление: распознаю бланк целиком, определяю форму…")
    lines = tesseract_lines(cv2.cvtColor(page.image, cv2.COLOR_BGR2GRAY), lang="rus+eng", psm=4)
    page_text = "\n".join(l.text for l in lines)
    form = detect_form(page_text, forms) or (forms[0] if form_id else None)
    if not form:
        ex = Extraction(doc_type="unknown", source_file=page.source)
        ex.notes.append("Тип формы не определён — добавьте YAML-конфиг в папку forms/.")
        return ex
    prefer = extract_from_text(page_text, form, page.source)
    for f in prefer.fields.values():
        f.confidence = 0.85
    ex = extract_from_scan(page, form, out_dir, vlm, handwritten_mode=handwritten, prefer=prefer)
    # поля без области на скане (например, «новому Администратору: …») — из текста страницы
    for k, f in prefer.fields.items():
        if k.split(".")[0] not in ex.fields:
            ex.fields[k] = FieldValue(f.value, f.confidence, "ocr-text", f.raw)
    ex.debug["page_text"] = page_text
    return ex
