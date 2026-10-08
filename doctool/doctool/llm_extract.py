"""Поля заявления — локальной текстовой моделью (Ollama) вместо регулярок бланка (0.7.0).

Включается на уровне дела: галка «Поля заявления — нейросетью» в веб-интерфейсе и окне Qt,
`python -m doctool check … --llm-fields [МОДЕЛЬ]` в командной строке (CaseInput.llm_fields). Без галки поля читаются
регулярками `text:` бланка, как в 0.6.0.

Как работает:
- модель получает текст заявления (текстовый слой PDF/DOCX или OCR страницы) и описания полей из YAML бланка
  (`llm:` у поля, `llm_context:` и `llm_example:` у бланка) и возвращает для каждого поля ДОСЛОВНУЮ цитату из текста;
- цитата ищется в тексте («привязка»). Нашлась — значением становится сам кусок документа (а не пересказ модели),
  дальше всё как раньше: разбор по `type` (FIELD_PARSERS), роли, проверки, вердикт.
  Не нашлась — модель что-то додумала: значение остаётся с уверенностью 0,55 → «ручная проверка»;
- проверки после ответа — по типу поля (apply_answer): печатный текст бланка, ФИО без имени, число цифр (`digits:`),
  границы «кем выдан», одно место текста — одно ФИО-поле, новый администратор ≠ заявитель, домены — сверка
  с прежним разборщиком по строкам списка.

Тип заявления, паспорт, области на скане, рукопись (VLM) — без изменений.
Если модель недоступна или не уложилась в предел — поля читаются регулярками бланка (с пометкой в замечаниях).

Параметры (переменные окружения, необязательно):
  DOCTOOL_LLM_TIMEOUT  предел на одно заявление, с (по умолчанию 240)
  DOCTOOL_LLM_CTX      размер контекста, токенов (по умолчанию 8192)
  DOCTOOL_LLM_PREDICT  предел ответа вместе с рассуждением, токенов (по умолчанию 6144)
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from . import parsers
from .models import Extraction
from .progress import log

PROMPT_VERSION = "3"
LOW_CONF = 0.55          # значение модели, которого нет в тексте
DEFAULT_MODEL = "qwen3:8b"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    model: str = DEFAULT_MODEL
    host: str = "http://127.0.0.1:11434"
    think: bool | None = True
    timeout: int = 240
    num_ctx: int = 8192
    num_predict: int = 6144
    cache_dir: str | None = "results/.llm_cache"


_active: contextvars.ContextVar[LLMConfig | None] = contextvars.ContextVar("doctool_llm", default=None)


def make_config(model: str | None = None, host: str = "http://127.0.0.1:11434", think: bool | None = True,
                cache_dir: str | None = "results/.llm_cache") -> LLMConfig:
    env = os.environ.get
    return LLMConfig(model=(model or DEFAULT_MODEL).strip(), host=host.rstrip("/"), think=think,
                     timeout=int(env("DOCTOOL_LLM_TIMEOUT") or 240), num_ctx=int(env("DOCTOOL_LLM_CTX") or 8192),
                     num_predict=int(env("DOCTOOL_LLM_PREDICT") or 6144), cache_dir=cache_dir)


@contextlib.contextmanager
def session(cfg: LLMConfig | None):
    """Режим «поля нейросетью» для одного дела (None — регулярками). Значение действует только в этом потоке/задаче,
    поэтому параллельные задачи веб-интерфейса не мешают друг другу."""
    token = _active.set(cfg)
    try:
        yield cfg
    finally:
        _active.reset(token)


def config() -> LLMConfig | None:
    return _active.get()


def enabled() -> bool:
    return config() is not None


def available(cfg: LLMConfig) -> str | None:
    """None — модель есть в Ollama; иначе — почему нет."""
    try:
        with urllib.request.urlopen(f"{cfg.host}/api/tags", timeout=3) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
    except Exception as e:  # noqa: BLE001
        return f"Ollama не отвечает ({cfg.host}): {e}"
    want = cfg.model if ":" in cfg.model else cfg.model + ":latest"
    if want not in names:
        return f"модели {cfg.model} нет в Ollama (ollama pull {cfg.model})"
    return None


# ------------------------------------------------------------------ запрос

RULES = """Ты извлекаешь данные из заявления клиента регистратора доменов. Текст получен из PDF или распознан OCR \
со скана, поэтому в нём бывают опечатки, лишние символы, разорванные строки, остатки печатей и подписей.

Правила:
1. Для каждого поля выпиши из текста фрагмент со значением ДОСЛОВНО — те же символы, что в тексте, включая опечатки \
распознавания. Не исправляй, не склоняй, не переставляй слова, не меняй формат дат и номеров.
2. Если значение разнесено по тексту на несколько кусков (между ними посторонние слова), перечисли куски через « … ».
3. Не включай в значение печатные подписи бланка (обычно в скобках: «(ФИО, дата рождения)», «(кем выдан)», \
«(подпись)»), названия полей («Паспорт:», «Адрес регистрации:») и подчёркивания.
4. Если поля в тексте нет или строка бланка не заполнена — пустая строка "" (для списка — пустой список). \
Не угадывай и не переноси значение из другого поля.
5. Заявитель — тот, кто подаёт заявление и передаёт права (текущий администратор: «от …», «Я, …», подпись внизу). \
Новый администратор — тот, кому передают права (после слов «новому Администратору»). Не путай их данные: ИНН, \
паспорт, контакты нового администратора не относятся к заявителю, и наоборот.
6. В тексте может быть посторонний текст (нотариальная надпись, опись, конверт, штамп) — бери значения только из \
самого заявления."""


def llm_fields(form: dict, only: list[str] | None = None) -> list[tuple[str, dict]]:
    """Поля бланка, которые читает модель: все, кроме `llm: false`."""
    out = []
    for key, spec in form["fields"].items():
        if spec.get("llm") is False or (only is not None and key not in only):
            continue
        out.append((key, spec))
    return out


def _count_key(key: str) -> str:
    return f"{key}__count"


def build_request(text: str, form: dict, only: list[str] | None = None) -> tuple[list[dict], dict]:
    fields = llm_fields(form, only)
    props: dict = {}
    lines = []
    for key, spec in fields:
        desc = spec.get("llm") or spec.get("title") or key
        desc = re.sub(r"\s+", " ", str(desc)).strip()
        if spec.get("type") == "domains":
            props[key] = {"type": "array", "items": {"type": "string"}}
            lines.append(f"- {key} (список): {desc}")
            props[_count_key(key)] = {"type": "string"}
            lines.append(f"- {_count_key(key)}: если заявитель сам указал, сколько всего доменов (например «всего 76 "
                         f"(семьдесят шесть) доменов», «в количестве 3 шт.»), — эта фраза дословно; иначе пусто.")
        else:
            props[key] = {"type": "string"}
            lines.append(f"- {key}: {desc}")
    schema = {"type": "object", "properties": props, "required": list(props)}
    ctx = re.sub(r"\s+", " ", str(form.get("llm_context") or form.get("title") or "")).strip()
    example = str(form.get("llm_example") or "").strip()
    user = (f"Документ: {ctx}\n\nПоля:\n" + "\n".join(lines) +
            (f"\n\nПример (вымышленный):\n{example}" if example else "") +
            f"\n\nТекст документа (между <<< и >>>):\n<<<\n{text}\n>>>\n\n"
            f"Ответь JSON с ключами: {', '.join(props)}.")
    return [{"role": "system", "content": RULES}, {"role": "user", "content": user}], schema


def ask(cfg: LLMConfig, messages: list[dict], schema: dict) -> tuple[dict, dict]:
    """Запрос с пределом cfg.timeout на всё. С рассуждением: если модель не уложилась (рассуждение съело предел
    токенов или времени), повтор без рассуждения в оставшееся время (не меньше 60 с)."""
    if not cfg.think:
        return _chat(cfg, messages, schema)
    t0 = time.time()
    first = LLMConfig(**{**cfg.__dict__, "timeout": max(30, int(cfg.timeout * 0.7))})
    try:
        return _chat(first, messages, schema)
    except LLMError as e:
        left = max(60, int(cfg.timeout - (time.time() - t0)))
        log(f"   с рассуждением не уложилась ({e}) — повтор без рассуждения, предел {left} с")
        data, meta = _chat(LLMConfig(**{**cfg.__dict__, "think": False, "timeout": left}), messages, schema)
        meta["retry_without_think"] = str(e)
        meta["seconds"] = round(time.time() - t0, 1)
        return data, meta


def _chat(cfg: LLMConfig, messages: list[dict], schema: dict) -> tuple[dict, dict]:
    payload = {"model": cfg.model, "messages": messages, "stream": False, "format": schema,
               "options": {"temperature": 0, "seed": 1, "num_ctx": cfg.num_ctx, "num_predict": cfg.num_predict}}
    if cfg.think is not None:
        payload["think"] = cfg.think
    key = hashlib.sha256((PROMPT_VERSION + json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
    cache = Path(cfg.cache_dir) / f"{key[:32]}.json" if cfg.cache_dir else None
    if cache is not None and cache.exists():
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            d["meta"]["cached"] = True
            return d["data"], d["meta"]
        except Exception:  # noqa: BLE001
            pass
    req = urllib.request.Request(f"{cfg.host}/api/chat", json.dumps({**payload, "keep_alive": "15m"}).encode(),
                                 {"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
            d = json.load(r)
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"{cfg.model}: {e}") from e
    if d.get("error"):
        raise LLMError(f"{cfg.model}: {d['error']}")
    msg = d.get("message") or {}
    content = (msg.get("content") or "").strip()
    meta = {"model": cfg.model, "think": cfg.think, "seconds": round(time.time() - t0, 1),
            "prompt_tokens": d.get("prompt_eval_count"), "output_tokens": d.get("eval_count"),
            "thinking_chars": len(msg.get("thinking") or ""), "done_reason": d.get("done_reason"), "cached": False}
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise json.JSONDecodeError("не объект", content, 0)
    except json.JSONDecodeError as e:
        raise LLMError(f"{cfg.model}: ответ не JSON ({meta['done_reason']}, {meta['output_tokens']} токенов)") from e
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"data": data, "meta": meta, "thinking": msg.get("thinking") or ""},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    return data, meta


# ------------------------------------------------------------------ привязка к тексту

def _proc(s: str) -> str:
    return s.lower().replace("ё", "е")


def prepare_text(text: str) -> str:
    """Текст для модели и для привязки: без длинных подчёркиваний и пустых строк."""
    t = re.sub(r"_{2,}", " ", text or "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def _ground_part(p: str, flat: str, pf: str, start: int) -> tuple[list[tuple[int, int]], int] | None:
    """Где в тексте кусок цитаты: [(начало, конец)…] и сходство. Сначала целиком (точно, затем нечётко ≥ 90),
    затем по словам: цитата, склеенная моделью из соседних кусков («4018 304191 10 апреля 2019 года», а в тексте
    между номером и датой — «кем выдан»), — подряд идущие слова находятся в тексте по порядку с разрывами
    не больше GAP символов."""
    q = _proc(p)
    i = pf.find(q, start)
    if i < 0:
        i = pf.find(q)
    if i >= 0:
        return [(i, i + len(q))], 100
    if len(p) >= 4:
        al = fuzz.partial_ratio_alignment(q, pf)
        if al is not None and al.score >= 90:
            a, b = al.dest_start, al.dest_end
            while a > 0 and pf[a - 1].isalnum() and pf[a].isalnum():        # до границ слов
                a -= 1
            while b < len(pf) and pf[b].isalnum() and pf[b - 1].isalnum():
                b += 1
            return [(a, b)], int(al.score)
    words = [w for w in (re.sub(r"^[^\w№+]+|[^\w)»\"]+$", "", x) for x in q.split()) if w]
    if len(words) < 2:
        return None
    spans: list[tuple[int, int]] = []
    pos, i = start, 0
    while i < len(words):
        best = None
        for j in range(len(words), i, -1):
            # слова подряд; между ними в тексте — пробелы и знаки препинания («Андреева ‚ д/р 13.03.1962»)
            rx = r"[^\w]{0,4}".join(re.escape(w) for w in words[i:j])
            m = re.compile(rx).search(pf, pos)
            if m and (not spans or m.start() - spans[-1][1] <= GAP):
                best = (m.start(), m.end(), j)
                break
        if best is None:
            return None
        spans.append(best[:2])
        pos, i = best[1], best[2]
    if len(spans) > 4:          # цитата рассыпалась на мелкие куски — это уже не цитата
        return None
    return spans, 95


GAP = 160


def ground(quote: str, flat: str) -> tuple[str | None, int]:
    text, score, _ = ground_at(quote, flat)
    return text, score


def ground_at(quote: str, flat: str) -> tuple[str | None, int, int]:
    """Кусок `flat` (текст с пробелами вместо переводов строк), совпадающий с цитатой модели, и сходство 0–100.
    Части, перечисленные через «…», ищутся по отдельности и должны найтись все."""
    parts = [p.strip(" ,;:") for p in re.split(r"\s*(?:…|\.\.\.)\s*", re.sub(r"\s+", " ", quote or ""))]
    parts = [p for p in parts if p]
    if not parts:
        return None, 0, -1
    pf = _proc(flat)
    out, worst, pos, first = [], 100, 0, -1
    for p in parts:
        r = _ground_part(p, flat, pf, pos)
        if r is None:
            return None, 0, -1
        spans, score = r
        first = spans[0][0] if first < 0 else first
        out += [flat[a:b] for a, b in spans]
        pos, worst = spans[-1][1], min(worst, score)
    return " ".join(out), worst, first


def _domain_in_text(d: str, nospace: str) -> bool:
    """Домен есть в тексте (переносы строк и пробелы внутри имени не мешают) и не только в адресах e-mail."""
    dn = re.sub(r"\s+", "", _proc(d)).strip(".,;")
    dn = re.sub(r"^(?:https?://)?(?:www\.)?", "", dn).rstrip("/")
    if len(dn) < 4:
        return False
    hits = [m.start() for m in re.finditer(re.escape(dn), nospace)]
    return any(i == 0 or nospace[i - 1] != "@" for i in hits)


# ------------------------------------------------------------------ правдоподобие значения

_FIO_TYPES = ("fio", "fio_date")
_NEW_ADMIN = ("new_admin_org", "new_admin_fio", "new_admin_contact", "new_admin_org_contact")


def implausible(ftype: str, span: str, form: dict, spec: dict | None = None) -> str | None:
    """Причина, по которой найденная в тексте цитата — не значение поля (None — значение правдоподобно):
    печатная подпись бланка («(Primary, Secondary DNS)»), «ФИО» без единого имени или отчества."""
    from . import formspec, names
    lab = formspec.labels_regex(form)
    core = span.strip().strip("()").strip()
    if lab is not None and core and lab.fullmatch(f"({core})"):
        return "печатная подпись бланка"
    digits = (spec or {}).get("digits")
    if digits and len(re.sub(r"\D", "", span)) not in (digits if isinstance(digits, list) else [digits]):
        return f"нужно {digits} цифр"
    printed = list(form.get("static_text") or []) + list((form.get("detect") or {}).get("keywords") or [])
    if len(core) >= 8 and any(fuzz.ratio(_proc(core), _proc(x.strip("()"))) >= 90 or
                              (len(core) >= 20 and _proc(core) in _proc(x)) for x in printed):
        return "печатный текст бланка"
    if ftype in _FIO_TYPES or (ftype == "fio_or_org" and not re.search(r"\b(?:ООО|АО|ПАО|ЗАО|ИП)\b", span)):
        toks = names.extract_fio(re.sub(r"^\s*(?:ИП|Индивидуальн\w+\s+предпринимател\w*)\b", "", span))
        initials = len(re.findall(r"\b[А-ЯЁ]\.", span)) >= 1
        known = sum(1 for t in toks if any(names.is_known(t, tag) for tag in ("Name", "Surn", "Patr")))
        if len(toks) < 2 or not (initials or any(names.is_patronymic(t) for t in toks) or known >= 2):
            return "не похоже на ФИО"
    return None


# ------------------------------------------------------------------ точка входа

def extract(text: str, form: dict, source_file: str, conf: float = 0.98, source: str = "text",
            only: list[str] | None = None, ex: Extraction | None = None) -> Extraction:
    """Поля бланка из текста моделью. `only` — только эти поля (например, раздел нового администратора),
    `ex` — дописать в готовое Extraction. LLMError — модель недоступна/не ответила."""
    cfg = config()
    if cfg is None:
        raise LLMError("модель не задана (DOCTOOL_LLM_FIELDS)")
    t = prepare_text(text)
    if len(re.sub(r"\W", "", t)) < 40:
        raise LLMError("текста для модели нет (пустая страница?)")
    messages, schema = build_request(t, form, only)
    log(f"Заявление: поля читает модель {cfg.model} ({len(schema['properties'])} полей, текст {len(t)} симв.)…")
    data, meta = ask(cfg, messages, schema)
    log(f"   модель ответила за {meta['seconds']} с" + (" (из кэша)" if meta.get("cached") else ""))
    if ex is None:
        ex = Extraction(doc_type=form["id"], source_file=source_file)
        ex.debug["mode"] = "llm"
    report = apply_answer(data, t, form, ex, conf=conf, source=source, only=only)
    ex.debug.setdefault("llm", []).append({"meta": meta, "fields": report, "only": only, "text": t})
    return ex


# ------------------------------------------------------------------ ответ модели → поля

_FIO_ALL = _FIO_TYPES + ("fio_or_org",)
_ISS_LEFT = re.compile(r"(?i)выдан[а-я]*\s*:?|(?<!\d)\d{1,2}\.\d{1,2}\.\d{4}(?:\s*(?:г\.|года|г(?=\s)))?|"
                       r"(?<!\d)\d{2}\s?\d{2}\s?№?\s?\d{6}(?!\d)")
_ISS_RIGHT = re.compile(r"(?i)[()]|(?<!\d)\d{1,2}\.\d{1,2}\.\d{2,4}|(?<!\d)\d{1,2}\s+(?:январ|феврал|март|апрел|ма[яй]|"
                        r"июн|июл|август|сентябр|октябр|ноябр|декабр)|код\s+подр|зарегистр|дата\s+выдачи|адрес|"
                        r"заявлени|\bИНН\b|(?<!\d)\d{6}(?!\d)|(?<!\d)\d{3}-\d{3}(?!\d)")


def _expand_issuer(a: int, b: int, flat: str) -> tuple[int, int]:
    """«Кем выдан» модель часто обрезает («УФМС России по Московской обл.» вместо «ТП в пос. Белоомуте О-НИЯ УФМС
    России по Московской обл.»). Расширяем до естественных границ: слева — до «выдан», даты или номера паспорта,
    справа — до даты, кода подразделения, адреса, индекса или печатной подписи в скобках (не дальше ~100 симв.)."""
    left = flat[max(0, a - 90):a]
    ms = list(_ISS_LEFT.finditer(left))
    if ms:
        gap = left[ms[-1].end():]
        g = gap.strip(" ,;:")
        if g and len(g) <= 80 and "(" not in gap and ")" not in gap and re.search(r"[А-Яа-яЁё]{2}", g) \
                and not re.search(r"(?i)паспорт|серия|рожд|дата", g):
            a = a - len(gap) + (len(gap) - len(gap.lstrip(" ,;:")))
    right = flat[b:b + 120]
    m = _ISS_RIGHT.search(right)
    if m:
        gap = right[:m.start()]
        g = gap.strip(" ,;:.")
        if g and len(g) <= 100 and re.search(r"[А-Яа-яЁё]{2}", g):
            b = b + len(gap.rstrip(" ,;:"))
    return a, b


def _occurrences(q: str, flat: str) -> list[int]:
    pf, qq = _proc(flat), _proc(re.sub(r"\s+", " ", q).strip())
    out, i = [], pf.find(qq)
    while i >= 0 and qq:
        out.append(i)
        i = pf.find(qq, i + 1)
    return out


def _domain_window_extra(items: list[str], t: str) -> list[str]:
    """Домены, которые модель пропустила. Строки текста с названными моделью доменами, плюс соседние строки, где тоже
    есть домены (без адресов e-mail), разбираются прежним разборщиком списка доменов; чего нет в ответе модели —
    пропущено."""
    lines = t.split("\n")
    plines = [_proc(x) for x in lines]
    hit = set()
    for d in items:
        dn = re.sub(r"\s+", "", _proc(d)).strip(".,;")
        dn = re.sub(r"^(?:https?://)?(?:www\.)?", "", dn)
        if len(dn) < 4:
            continue
        for i, pl in enumerate(plines):
            if dn in re.sub(r"\s+", "", pl):
                hit.add(i)
    if not hit:
        return []

    def listy(i: int) -> bool:
        return "@" not in lines[i] and bool(parsers.find_domains(lines[i]))

    a, b = min(hit), max(hit)
    while a > 0 and listy(a - 1):
        a -= 1
    while b + 1 < len(lines) and listy(b + 1):
        b += 1
    window = "\n".join(lines[a:b + 1])
    pt = _proc(t)
    have = set(parsers.find_domains("\n".join(items)))
    out = []
    for d in parsers.parse_domain_list(window).domains:
        if d in have or ("@" + d) in pt or d in out:
            continue
        out.append(d)
    return out


_NEW_ADMIN_PERSON = ("new_admin_fio", "new_admin_inline")


def _same_person(span: str, ex: Extraction) -> bool:
    """Новый администратор — это сам заявитель (модель взяла ФИО из «Я, …» или подписи)."""
    from . import names
    me = {names.norm(x) for x in (ex.get("applicant_header"), ex.get("applicant_fio")) if x}
    me |= {names.norm(names.to_nominative(x)) for x in (ex.get("applicant_fio"),) if x}
    toks = names.extract_fio(re.sub(r"^\s*ИП\b", "", span))
    if len(toks) < 2:
        return False
    cand = {names.norm(" ".join(names.ordered(toks))), names.norm(names.to_nominative(" ".join(toks)))}
    return bool(me & cand)


def _drop(ex: Extraction, key: str) -> None:
    for k in [k for k in ex.fields if k == key or k.startswith(key + ".")]:
        del ex.fields[k]


def apply_answer(data: dict, t: str, form: dict, ex: Extraction, conf: float = 0.98, source: str = "text",
                 only: list[str] | None = None) -> dict:
    """Ответ модели {поле: цитата} → значения полей Extraction (через прежний разбор по type). Возвращает отчёт
    по полям: что ответила модель, где нашлось в тексте, что отброшено и почему."""
    from . import names
    from .application import _store, _usable   # здесь, чтобы не было цикла импортов

    flat = re.sub(r"\s+", " ", t)
    nospace = re.sub(r"\s+", "", _proc(t)).replace("\u00ad", "")
    src = f"llm-{source}"
    report: dict = {}
    missing: list[str] = []
    fio_at: dict[int, str] = {}
    for key, spec in llm_fields(form, only):
        val = data.get(key)
        ftype = spec.get("type", "text")
        if ftype == "domains":
            items = [str(x).strip() for x in (val or []) if str(x).strip()] if isinstance(val, list) else \
                [x for x in re.split(r"[\s,;]+", str(val or "")) if x]
            items = list(dict.fromkeys(items))
            if not items:
                report[key] = {"answer": [], "grounded": None}
                continue
            bad = [d for d in items if not _domain_in_text(d, nospace)]
            extra = _domain_window_extra(items, t)
            count_q = str(data.get(_count_key(key)) or "")
            count_span, _ = ground(count_q, flat) if count_q else (None, 0)
            raw = "\n".join(items + extra) + (f"\n{count_span}" if count_span else "")
            _store(ex, key, ftype, raw, conf, src)
            unsure = list(dict.fromkeys([re.sub(r"\s+", "", d.lower()) for d in bad] + extra))
            if unsure:
                ex.set(f"{key}.uncertain", unsure, conf, src)
            if bad:
                missing.append(spec.get("title", key))
            if extra:
                ex.notes.append(f"«{spec.get('title', key)}»: модель не назвала домены, которые есть в списке "
                                f"заявления: {', '.join(extra)} — добавлены, нужна ручная проверка.")
            report[key] = {"answer": items, "not_in_text": bad, "added_from_text": extra, "count": count_q}
            continue
        q = str(val or "").strip()
        if not q:
            report[key] = {"answer": ""}
            continue
        span, score, at = ground_at(q, flat)
        if span and ftype == "issuer" and flat[at:at + len(span)] == span:
            a, b = _expand_issuer(at, at + len(span), flat)
            if (a, b) != (at, at + len(span)):
                report.setdefault(key, {})["expanded_from"] = span
                span, at = flat[a:b], a
        why = implausible(ftype, span or q, form, spec)
        if not why and span and key in _NEW_ADMIN_PERSON and _same_person(span, ex):
            why = "совпадает с заявителем"
        if not why and span and ftype in _FIO_ALL:
            if at in fio_at and fio_at[at] == "new_admin_inline" and key in _NEW_ADMIN_PERSON:
                _drop(ex, "new_admin_inline")      # строка «новому Администратору: …» и есть таблица — берём поле таблицы
                report["new_admin_inline"] = {**report.get("new_admin_inline", {}), "dropped": f"то же, что {key}"}
                del fio_at[at]
            if at in fio_at:      # тот же кусок текста уже занят другим полем: ищем другое вхождение
                free = [i for i in _occurrences(span, flat) if i not in fio_at]
                if free:
                    at = free[0]
                else:
                    why = f"тот же кусок текста, что у поля {fio_at[at]}"   # «ФИО у подписи» = ФИО из «Я, …»
            if not why:
                fio_at[at] = key
        if why:
            report[key] = {**report.get(key, {}), "answer": q, "span": span, "score": score, "dropped": why}
            continue
        if span:
            _store(ex, key, ftype, span, conf, src)
            if key in _NEW_ADMIN and key in ex.fields and not _usable(key, ex.fields[key].value):
                _drop(ex, key)
                report[key] = {"answer": q, "span": span, "score": score, "dropped": "не похоже на значение поля"}
                continue
        else:
            _store(ex, key, ftype, q, min(conf, LOW_CONF), src + "?")
            missing.append(spec.get("title", key))
        report[key] = {**report.get(key, {}), "answer": q, "span": span, "score": score}
    if missing:
        ex.notes.append("Модель вернула значения, которых нет в тексте заявления (нужна ручная проверка): "
                        + ", ".join(f"«{m}»" for m in missing) + ".")
    return report
