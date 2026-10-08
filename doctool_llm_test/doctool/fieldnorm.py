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
