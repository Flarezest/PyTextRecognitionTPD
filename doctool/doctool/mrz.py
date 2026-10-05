"""Разбор машиночитаемой зоны (MRZ) паспортов.

Поддерживается формат TD3 (2 строки по 44 символа):
- внутренний паспорт РФ (тип "PN", страна "RUS") — с обратной транслитерацией ФИО
  в кириллицу и извлечением даты выдачи и кода подразделения из необязательного поля;
- заграничные паспорта любых стран (ICAO 9303).

OCR часто путает похожие символы (O/0, I/1, B/8 …). Цифровые поля исправляются
перебором замен, пока не сойдётся контрольная цифра.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date

# Таблица транслитерации MRZ внутреннего паспорта РФ (кириллица -> латиница/цифры).
RU_MRZ_TABLE = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "2", "Ж": "J",
    "З": "Z", "И": "I", "Й": "Q", "К": "K", "Л": "L", "М": "M", "Н": "N", "О": "O",
    "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U", "Ф": "F", "Х": "H", "Ц": "C",
    "Ч": "3", "Ш": "4", "Щ": "W", "Ъ": "X", "Ы": "Y", "Ь": "9", "Э": "6", "Ю": "7",
    "Я": "8",
}
RU_MRZ_REVERSE = {v: k for k, v in RU_MRZ_TABLE.items()}

_WEIGHTS = (7, 3, 1)
_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "Z": "2",
             "S": "5", "B": "8", "G": "6", "T": "7", "A": "4"}
_TO_ALPHA = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}


def check_digit(s: str) -> str:
    total = 0
    for i, ch in enumerate(s):
        if ch.isdigit():
            v = int(ch)
        elif "A" <= ch <= "Z":
            v = ord(ch) - 55
        else:  # '<'
            v = 0
        total += v * _WEIGHTS[i % 3]
    return str(total % 10)


def _fix_numeric(s: str) -> str:
    return "".join(_TO_DIGIT.get(c, c) for c in s)


_CONFUSABLE = {"0": "86", "1": "7", "7": "1", "8": "06", "6": "085", "5": "69", "9": "5",
               "3": "8", "2": "7", "4": "1"}


def _field_candidates(value: str, cd: str, numeric: bool, max_changes: int = 2) -> list[tuple[str, int]]:
    """Варианты поля, при которых сходится контрольная цифра (по возрастанию числа замен)."""
    cd = _fix_numeric(cd)
    if numeric:
        value = _fix_numeric(value)
    out = []
    if check_digit(value) == cd:
        out.append((value, 0))
    positions = [i for i, c in enumerate(value) if c in _CONFUSABLE]
    for n in range(1, max_changes + 1):
        for pos in itertools.combinations(positions, n):
            for repl in itertools.product(*(_CONFUSABLE[value[p]] for p in pos)):
                cand = list(value)
                for p, r in zip(pos, repl):
                    cand[p] = r
                cand = "".join(cand)
                if check_digit(cand) == cd:
                    out.append((cand, n))
        if len(out) > 30:
            break
    return out or [(value, -1)]   # -1: исправить не удалось


def _yymmdd(s: str, future_ok: bool = False) -> date | None:
    if not re.fullmatch(r"\d{6}", s):
        return None
    yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:])
    today = date.today()
    year = 2000 + yy
    if not future_ok and year > today.year:
        year -= 100
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


def _decode_name_ru(s: str) -> str:
    return "".join(RU_MRZ_REVERSE.get(c, c) for c in s)


@dataclass
class MRZResult:
    raw: list[str]
    doc_type: str = ""
    country: str = ""
    surname: str = ""
    given_names: str = ""
    patronymic: str = ""
    number: str = ""
    nationality: str = ""
    birth_date: date | None = None
    sex: str = ""
    expiry_date: date | None = None
    # только для паспорта РФ
    series: str = ""
    issue_date: date | None = None
    department_code: str = ""
    checks: dict = field(default_factory=dict)
    corrections: int = 0

    @property
    def valid(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        for k in ("birth_date", "expiry_date", "issue_date"):
            d[k] = d[k].strftime("%d.%m.%Y") if d[k] else None
        d["valid"] = self.valid
        return d


def normalize_line(line: str) -> str:
    line = line.upper().replace(" ", "")
    # кириллические двойники латиницы, которые возвращает OCR с русской моделью
    line = line.translate(str.maketrans("АВЕКМНОРСТХУ«‹", "ABEKMHOPCTXY<<"))
    line = re.sub(r"[^A-Z0-9<]", "<", line)
    return line


def find_mrz_lines(text_lines: list[str]) -> list[str] | None:
    """Ищет две соседние строки, похожие на MRZ TD3."""
    cands = [normalize_line(l) for l in text_lines]
    for i in range(len(cands) - 1):
        a, b = cands[i], cands[i + 1]
        if a.startswith("P") and a.count("<") >= 5 and len(a) >= 36 and len(b) >= 36 and re.search(r"\d{6}", b):
            return [a.ljust(44, "<")[:44], b.ljust(44, "<")[:44]]
    return None


def clean_name_field(s: str) -> str:
    """Tesseract часто читает заполнитель «<» как «K»: чистим хвост и стыки."""
    s = re.sub(r"[K<]*$", "", s)                      # хвост из K/<
    s = re.sub(r"(?<=<)K(?=<)|K(?=<<)|(?<=<<)K", "<", s)  # одиночные K среди <
    s = re.sub(r"<K<|K<<|<<K", "<<", s)
    if "<<" not in s:                                  # разделитель фамилии прочитан как «K<»/«<K»
        s = re.sub(r"K<|<K", "<<", s, count=1)
    return s


def split_patronymic(given: str) -> tuple[str, str]:
    """«EKATERINAKSERGEEVNA» -> («ЕКАТЕРИНА», «СЕРГЕЕВНА»), если разделитель прочитан как K."""
    for m in re.finditer("K", given):
        left, right = given[:m.start()], given[m.end():]
        if len(left) >= 2 and re.search(r"(VI3|VNA|I3NA|OGLY|KYZY)$", right):
            return left, right
    return given, ""


def parse_td3(line1: str, line2: str) -> MRZResult:
    l1 = normalize_line(line1).ljust(44, "<")[:44]
    l2 = normalize_line(line2).ljust(44, "<")[:44]
    r = MRZResult(raw=[l1, l2])
    r.doc_type = l1[0:2].replace("<", "")
    r.country = l1[2:5]
    is_ru_internal = r.country == "RUS" and r.doc_type == "PN"

    names = clean_name_field(l1[5:])
    parts = names.split("<<", 1)
    surname = parts[0].replace("<", " ").strip()
    rest = [p for p in (parts[1].split("<") if len(parts) > 1 else []) if p]

    number, cd_num = l2[0:9], l2[9]
    r.nationality = l2[10:13]
    dob, cd_dob = l2[13:19], l2[19]
    r.sex = {"M": "М", "F": "Ж"}.get(l2[20], l2[20]) if is_ru_internal else l2[20]
    exp, cd_exp = l2[21:27], l2[27]
    opt, cd_opt = l2[28:42], l2[42]
    cd_all = l2[43]

    if is_ru_internal:
        opt = _fix_numeric(opt[:13]) + opt[13]
    fields = {
        "number": _field_candidates(number, cd_num, numeric=is_ru_internal),
        "birth_date": _field_candidates(dob, cd_dob, numeric=True),
        "expiry_date": _field_candidates(exp, cd_exp, numeric=True) if exp.strip("<") else [(exp, 0)],
        "optional": (_field_candidates(opt, cd_opt, numeric=False)
                     if is_ru_internal or opt.strip("<") else [(opt, 0)]),
    }
    cds = {"number": _fix_numeric(cd_num), "birth_date": _fix_numeric(cd_dob),
           "expiry_date": _fix_numeric(cd_exp) if exp.strip("<") else cd_exp,
           "optional": _fix_numeric(cd_opt) if (is_ru_internal or opt.strip("<")) else cd_opt}
    # выбираем комбинацию с минимумом исправлений, при которой сходится и общая контрольная цифра
    best = None
    for combo in itertools.product(*fields.values()):
        vals = dict(zip(fields, combo))
        composite = "".join(vals[k][0] + cds[k] for k in fields)
        ok_all = check_digit(composite) == _fix_numeric(cd_all)
        changes = sum(abs(v[1]) for v in vals.values()) + (0 if ok_all else 100)
        if best is None or changes < best[0]:
            best = (changes, vals, ok_all)
    _, vals, ok_all = best
    number, dob, exp, opt = (vals[k][0] for k in ("number", "birth_date", "expiry_date", "optional"))
    ok_num, ok_dob, ok_exp, ok_opt = (vals[k][1] >= 0 for k in ("number", "birth_date", "expiry_date", "optional"))
    r.corrections = sum(max(0, v[1]) for v in vals.values())

    r.checks = {"number": ok_num, "birth_date": ok_dob, "expiry_date": ok_exp,
                "optional": ok_opt, "composite": ok_all}
    r.birth_date = _yymmdd(dob)
    r.expiry_date = _yymmdd(exp, future_ok=True)

    if is_ru_internal:
        # номер: 3 первые цифры серии + 6 цифр номера; 4-я цифра серии — первый символ доп. поля
        r.series = number[:3] + opt[0]
        r.number = number[3:]
        r.issue_date = _yymmdd(opt[1:7])
        dc = opt[7:13]
        r.department_code = f"{dc[:3]}-{dc[3:]}" if dc.isdigit() else ""
        r.surname = _decode_name_ru(surname.replace(" ", ""))
        if len(rest) == 1:
            rest = [x for x in split_patronymic(rest[0]) if x]
        if rest:
            r.given_names = _decode_name_ru(rest[0])
        if len(rest) > 1:
            r.patronymic = _decode_name_ru(" ".join(rest[1:]))
    else:
        r.number = number.rstrip("<")
        r.surname = surname
        r.given_names = " ".join(rest)
    return r
