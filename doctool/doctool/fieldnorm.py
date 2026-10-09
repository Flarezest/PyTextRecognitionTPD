"""Сравнение значений из двух источников «совпадает / расходится» (зелёный / красный в интерфейсе).

Используется там, где два источника показываются рядом:
  * Sd домена ↔ данные ЕСИА (domains.esia_compare, окно ESIA в таблице доменов и в выгрузке);
  * базовая анкета аккаунта ↔ заявление (account.compare_owner, вкладка «Смена владельца ЛК»).

Вид значения (kind) определяет, как его привести к сравнимому виду:
  text    — без учёта регистра, ё/е, пробелов и знаков препинания;
  date    — дата в любом виде («24.08.2021», «2021-08-24», «24 августа 2021»);
  digits  — только цифры (серия и номер паспорта: «4021 925790» = «4021925790»);
  phone   — цифры, 8XXXXXXXXXX и XXXXXXXXXX (10 цифр) приводятся к 7XXXXXXXXXX;
  country — код страны: «RUS» = «RU» = «Россия»;
  issuer  — «кем выдан»: одинаковый текст → совпадает, похожий (≥ 85 %) → «похоже», иначе расходится.

С 0.7.2 — для юрлиц (Sd группы ru_org ↔ ЕСИА организации):
  org     — название организации: организационно-правовая форма сокращением и полностью — одно и то же
            («ООО «Ромашка»» = «ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "РОМАШКА"»), само название должно совпасть;
  house   — дом: «11» = «Д. 11» = «дом 11»; совпал только номер, а корпус/строение записаны иначе → «похоже»;
  street  — улица: «ул. Промышленная» = «УЛ. ПРОМЫШЛЕННАЯ» = «Промышленная улица»; тот же адрес с другим
            типом улицы или похожее название (≥ 85 %) → «похоже».
"""
from __future__ import annotations

import re

from . import parsers

OK, WARN, FAIL, NONE = "ok", "warn", "fail", ""

# ISO 3166-1: alpha-3 → alpha-2 (ЕСИА пишет гражданство как RUS, manager — RU)
_ISO3 = dict((x[:3], x[3:]) for x in (
    "ABWAW AFGAF AGOAO AIAAI ALAAX ALBAL ANDAD AREAE ARGAR ARMAM ASMAS ATAAQ ATFTF ATGAG AUSAU AUTAT AZEAZ BDIBI "
    "BELBE BENBJ BESBQ BFABF BGDBD BGRBG BHRBH BHSBS BIHBA BLMBL BLRBY BLZBZ BMUBM BOLBO BRABR BRBBB BRNBN BTNBT "
    "BVTBV BWABW CAFCF CANCA CCKCC CHECH CHLCL CHNCN CIVCI CMRCM CODCD COGCG COKCK COLCO COMKM CPVCV CRICR CUBCU "
    "CUWCW CXRCX CYMKY CYPCY CZECZ DEUDE DJIDJ DMADM DNKDK DOMDO DZADZ ECUEC EGYEG ERIER ESHEH ESPES ESTEE ETHET "
    "FINFI FJIFJ FLKFK FRAFR FROFO FSMFM GABGA GBRGB GEOGE GGYGG GHAGH GIBGI GINGN GLPGP GMBGM GNBGW GNQGQ GRCGR "
    "GRDGD GRLGL GTMGT GUFGF GUMGU GUYGY HKGHK HMDHM HNDHN HRVHR HTIHT HUNHU IDNID IMNIM INDIN IOTIO IRLIE IRNIR "
    "IRQIQ ISLIS ISRIL ITAIT JAMJM JEYJE JORJO JPNJP KAZKZ KENKE KGZKG KHMKH KIRKI KNAKN KORKR KWTKW LAOLA LBNLB "
    "LBRLR LBYLY LCALC LIELI LKALK LSOLS LTULT LUXLU LVALV MACMO MAFMF MARMA MCOMC MDAMD MDGMG MDVMV MEXMX MHLMH "
    "MKDMK MLIML MLTMT MMRMM MNEME MNGMN MNPMP MOZMZ MRTMR MSRMS MTQMQ MUSMU MWIMW MYSMY MYTYT NAMNA NCLNC NERNE "
    "NFKNF NGANG NICNI NIUNU NLDNL NORNO NPLNP NRUNR NZLNZ OMNOM PAKPK PANPA PCNPN PERPE PHLPH PLWPW PNGPG POLPL "
    "PRIPR PRKKP PRTPT PRYPY PSEPS PYFPF QATQA REURE ROURO RUSRU RWARW SAUSA SDNSD SENSN SGPSG SGSGS SHNSH SJMSJ "
    "SLBSB SLESL SLVSV SMRSM SOMSO SPMPM SRBRS SSDSS STPST SURSR SVKSK SVNSI SWESE SWZSZ SXMSX SYCSC SYRSY TCATC "
    "TCDTD TGOTG THATH TJKTJ TKLTK TKMTM TLSTL TONTO TTOTT TUNTN TURTR TUVTV TWNTW TZATZ UGAUG UKRUA UMIUM URYUY "
    "USAUS UZBUZ VATVA VCTVC VENVE VGBVG VIRVI VNMVN VUTVU WLFWF WSMWS YEMYE ZAFZA ZMBZM ZWEZW").split())
_RU_NAMES = {"РОССИЯ", "РФ", "РОССИЙСКАЯ ФЕДЕРАЦИЯ", "RUSSIA", "RUSSIAN FEDERATION"}


def country_code(s: str) -> str:
    """«RUS» / «RU» / «Россия» → «RU». Неизвестное — как есть (в верхнем регистре)."""
    v = re.sub(r"\s+", " ", (s or "").strip().upper().replace("Ё", "Е"))
    if v in _RU_NAMES:
        return "RU"
    return _ISO3.get(v, v)


def phone_digits(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    elif len(d) == 10 and d[0] == "9":
        d = "7" + d
    return d


def norm(kind: str, value) -> str:
    """Значение в сравнимом виде."""
    s = "" if value is None else str(value)
    if kind == "date":
        d = parsers.parse_date(s)
        return d.strftime("%d.%m.%Y") if d else re.sub(r"\s+", "", s)
    if kind == "digits":
        return re.sub(r"\D", "", s)
    if kind == "phone":
        return phone_digits(s)
    if kind == "country":
        return country_code(s)
    # text, issuer
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zа-я]+", " ", s.lower().replace("ё", "е"))).strip()


def same(kind: str, a, b) -> tuple[str, str]:
    """Сравнение двух значений: (статус, пояснение). Статус NONE — сравнивать не с чем (одно из значений пустое)."""
    special = {"org": same_org, "house": same_house, "street": same_street}.get(kind)
    if special is not None:
        return special(a, b)
    na, nb = norm(kind, a), norm(kind, b)
    if not na or not nb:
        return NONE, ""
    if na == nb:
        return OK, ""
    if kind == "issuer":
        from .compare import issuer_similarity
        sim = issuer_similarity(str(a), str(b))
        if sim >= 85:
            return WARN, f"написано по-разному, сходство {sim:.0f}%"
        return FAIL, f"сходство {sim:.0f}%"
    return FAIL, ""


# ------------------------------------------------------------------ юрлица: название, дом, улица (0.7.2)

def _words(s) -> str:
    """Нижний регистр, ё → е, всё кроме букв и цифр — пробел."""
    t = ("" if s is None else str(s)).lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zа-я]+", " ", t)).strip()


# Организационно-правовые формы: полное название и сокращения → множество кодов. Формы совпадают, если у них
# есть общий код. «АО», «НАО», «НПАО» и «(непубличное) акционерное общество» — одно и то же (с 2014 г. в ЕГРЮЛ
# непубличное АО называется «АКЦИОНЕРНОЕ ОБЩЕСТВО»). «Хозяйственное товарищество» — полное товарищество или
# товарищество на вере. ОАО, ЗАО и ПАО — отдельные формы (смена ЗАО → АО — повод обновить Sd).
_HT = frozenset({"ХТ", "ПТ", "ТВ"})
_ORG_PHRASES = sorted((
    ("общество с ограниченной ответственностью", frozenset({"ООО"})),
    ("общество с дополнительной ответственностью", frozenset({"ОДО"})),
    ("непубличное акционерное общество", frozenset({"АО"})),
    ("акционерное общество", frozenset({"АО"})),
    ("публичное акционерное общество", frozenset({"ПАО"})),
    ("открытое акционерное общество", frozenset({"ОАО"})),
    ("закрытое акционерное общество", frozenset({"ЗАО"})),
    ("производственный кооператив", frozenset({"ПК"})),
    ("потребительский кооператив", frozenset({"ПОТРК"})),
    ("хозяйственное товарищество", _HT),
    ("полное товарищество", frozenset({"ПТ", "ХТ"})),
    ("товарищество на вере", frozenset({"ТВ", "ХТ"})),
    ("коммандитное товарищество", frozenset({"ТВ", "ХТ"})),
    ("автономная некоммерческая организация", frozenset({"АНО"})),
    ("федеральное государственное унитарное предприятие", frozenset({"ФГУП"})),
    ("государственное унитарное предприятие", frozenset({"ГУП"})),
    ("муниципальное унитарное предприятие", frozenset({"МУП"})),
), key=lambda x: -len(x[0]))
_ORG_ABBR = {
    "ооо": frozenset({"ООО"}), "одо": frozenset({"ОДО"}), "ао": frozenset({"АО"}), "нао": frozenset({"АО"}),
    "нпао": frozenset({"АО"}), "пао": frozenset({"ПАО"}), "оао": frozenset({"ОАО"}), "зао": frozenset({"ЗАО"}),
    "пк": frozenset({"ПК", "ПОТРК"}), "хт": _HT, "пт": frozenset({"ПТ", "ХТ"}), "тв": frozenset({"ТВ", "ХТ"}),
    "кт": frozenset({"ТВ", "ХТ"}), "ано": frozenset({"АНО"}), "фгуп": frozenset({"ФГУП"}), "гуп": frozenset({"ГУП"}),
    "муп": frozenset({"МУП"}),
}
_QUOTES = "\"«»“”„‟'‘’`"


def org_parts(s) -> tuple[list, str]:
    """Название организации → ([формы — множества кодов], название без формы и кавычек).

    Если есть кавычки, название — текст в кавычках (вложенные кавычки убираются), форма — снаружи. Без кавычек
    полное название формы ищется где угодно, сокращение — только в начале или в конце («ООО Ромашка», «Ромашка, ООО»):
    внутри названия сокращение может оказаться словом («АО "ТВ Центр"»)."""
    s = "" if s is None else str(s)
    qs = [i for i, c in enumerate(s) if c in _QUOTES]
    quoted = len(qs) >= 2
    name, outside = (s[qs[0] + 1:qs[-1]], s[:qs[0]] + " " + s[qs[-1] + 1:]) if quoted else (s, "")
    forms: list = []

    def phrases(t: str) -> list[str]:
        w = f" {_words(t)} "
        for phrase, codes in _ORG_PHRASES:
            while f" {phrase} " in w:
                forms.append(codes)
                w = w.replace(f" {phrase} ", " ", 1)
        return w.split()

    if quoted:
        forms.extend(_ORG_ABBR[t] for t in phrases(outside) if t in _ORG_ABBR)
        words = _words(name).split()
    else:
        words = phrases(name)
        while words and words[0] in _ORG_ABBR:
            forms.append(_ORG_ABBR[words.pop(0)])
        while words and words[-1] in _ORG_ABBR:
            forms.append(_ORG_ABBR[words.pop()])
    return forms, " ".join(words)


def _form_ru(forms: list) -> str:
    return ", ".join("/".join(sorted(f)) for f in forms) or "—"


def same_org(a, b) -> tuple[str, str]:
    """Название организации: форма (сокращение = полное название) + само название (без учёта регистра, кавычек,
    знаков препинания)."""
    fa, na = org_parts(a)
    fb, nb = org_parts(b)
    if not (fa or na) or not (fb or nb):
        return NONE, ""
    if na != nb:
        return FAIL, "название организации не совпадает"
    if fa and fb:
        if any(x & y for x in fa for y in fb):
            return OK, ""
        return FAIL, f"разная организационно-правовая форма: {_form_ru(fa)} ≠ {_form_ru(fb)}"
    if fa or fb:
        return WARN, "название совпадает, организационно-правовая форма указана только с одной стороны"
    return OK, ""


_HOUSE_MARK = {"к": "к", "корп": "к", "корпус": "к", "кор": "к", "стр": "стр", "строение": "стр", "с": "стр",
               "лит": "лит", "литер": "лит", "литера": "лит"}
_HOUSE_DROP = {"д", "дом", "n"}


def house_key(s) -> list[str]:
    """«Д. 11», «дом 11», «11» → ['11']; «д. 5, корп. 2» → ['5', 'к', '2']; «11 А» → ['11а']."""
    t = ("" if s is None else str(s)).lower().replace("ё", "е")
    t = re.sub(r"[^0-9a-zа-я/]+", " ", t)
    t = re.sub(r"(?<=[a-zа-я])(?=\d)", " ", t)          # «д11» → «д 11», «корп2» → «корп 2»
    out: list[str] = []
    for tok in t.split():
        if tok in _HOUSE_DROP:
            continue
        if tok in _HOUSE_MARK:
            out.append(_HOUSE_MARK[tok])
        elif len(tok) == 1 and tok.isalpha() and out and out[-1][:1].isdigit():
            out[-1] += tok                                    # «11 а» → «11а»
        else:
            out.append(tok)
    return out


def same_house(a, b) -> tuple[str, str]:
    ka, kb = house_key(a), house_key(b)
    if not ka or not kb:
        return NONE, ""
    if ka == kb:
        return OK, ""
    num = lambda k: (re.match(r"\d+", k[0]) or [""])[0]  # noqa: E731
    if num(ka) and num(ka) == num(kb):
        return WARN, "номер дома совпадает, корпус, строение или литера записаны по-разному"
    return FAIL, ""


# Типы улиц: полное название и сокращения → одно обозначение. Порядок важен: «пр-д» (проезд) раньше «пр» (проспект).
_STREET_TYPES = (
    ("улица|ул", "ул"), ("проезд|пр-?д", "пр-д"), ("проспект|пр-?кт|просп|пр-?т|пр", "пр-кт"), ("переулок|пер", "пер"),
    ("бульвар|б-?р|бул", "б-р"), ("шоссе|ш", "ш"), ("площадь|пл", "пл"), ("набережная|наб", "наб"), ("тупик|туп", "туп"),
    ("аллея|ал", "ал"), ("микрорайон|мкр-?н|мкр", "мкр"), ("квартал|кв-?л", "кв-л"), ("территория|тер", "тер"),
    ("линия|лин", "лин"), ("дорога|дор", "дор"), ("тракт", "тракт"), ("километр|км", "км"), ("спуск", "спуск"),
    ("въезд", "въезд"), ("просека", "просека"), ("городок", "городок"),
)
_STREET_RX = [(re.compile(rf"(?<![0-9a-zа-я])(?:{p})\.?(?![0-9a-zа-я])"), c) for p, c in _STREET_TYPES]


def street_parts(s) -> tuple[set, str]:
    """«ул. Промышленная» → ({'ул'}, 'промышленная'); «Ленинский пр-кт» → ({'пр-кт'}, 'ленинский')."""
    t = ("" if s is None else str(s)).lower().replace("ё", "е")
    types: set = set()
    for rx, canon in _STREET_RX:
        t, n = rx.subn(" ", t)
        if n:
            types.add(canon)
    words = [w for w in _words(t).split() if w not in ("им", "имени")]
    return types, " ".join(words)


def same_street(a, b) -> tuple[str, str]:
    ta, na = street_parts(a)
    tb, nb = street_parts(b)
    if not na or not nb:
        return NONE, ""
    if na == nb:
        if ta and tb and not ta & tb:
            return WARN, f"тип улицы разный: {', '.join(sorted(ta))} ≠ {', '.join(sorted(tb))}"
        return OK, ""
    from rapidfuzz import fuzz
    sim = fuzz.token_set_ratio(na, nb)
    if sim >= 85:
        return WARN, f"написано по-разному, сходство {sim:.0f}%"
    return FAIL, f"сходство {sim:.0f}%"
