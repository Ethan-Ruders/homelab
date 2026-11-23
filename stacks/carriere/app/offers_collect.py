"""Multi-source offer collector for Homelab Career Radar (LBA, Thales, Sopra, Nokia, Arkea, Adzuna)."""
from __future__ import annotations

import calendar
from datetime import date, timedelta
import hashlib
import html
import logging
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from .geo import ZONES, classify_travel_time, ensure_zones_isochrones

logger = logging.getLogger(__name__)

LBA_SEARCH_URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"
THALES_JOBS_URL = "https://thales.wd3.myworkdayjobs.com/wday/cxs/thales/Careers/jobs"
THALES_BASE_URL = "https://thales.wd3.myworkdayjobs.com"
SOPRA_JOBS_URL = "https://api.smartrecruiters.com/v1/companies/SopraSteria1/postings"
NOKIA_JOBS_URL = "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
ARKEA_RSS_URL = "https://cmarkea-recrute.talent-soft.com/Handlers/OfferRss.ashx"

ZONE_CITIES: dict[str, tuple[str, ...]] = {
    "loperhet": (
        "LOPERHET", "BREST", "PLOUZANE", "GUIPAVAS", "GOUESNOU", "LANDERNEAU",
        "PLOUGASTEL", "RELECQ", "DAOULAS", "DIRINON", "PLABENNEC", "BOHARS",
        "GUILERS", "MILIZAC", "SAINT RENAN", "LOCMARIA", "FINISTERE", "29",
    ),
    "lannion": (
        "LANNION", "PERROS", "PLEUMEUR", "TREBEURDEN", "TREGUIER", "CAVAN",
        "BEGARD", "TONQUEDEC", "PLESTIN", "LOUANNEC", "ROSPEZ", "PLOULEC",
        "COTES D ARMOR", "COTES-D'ARMOR", "22",
    ),
    "caen": (
        "CAEN", "COLOMBELLES", "HEROUVILLE", "MONDEVILLE", "IFS", "CARPIQUET",
        "CONTEST", "FLEURY", "CORMELLES", "OUISTREHAM", "BIEVILLE", "GIBERVILLE",
        "BRETTEVILLE", "FALAISE", "BAYEUX", "CALVADOS", "14",
    ),
}

IT_RELEVANCE_RE = re.compile(
    r"(?:"
    r"\b(?:CYBER\w*|PENTEST\w*|SOC|SIEM|DEVSECOPS|SECOPS|RSSI|CRYPTOGRAPH\w*|"
    r"INFORMATIQUE\w*|LOGICIEL\w*|SOFTWARE|FULL\s*-?\s*STACK|BACK\s*-?\s*END|FRONT\s*-?\s*END|"
    r"DEVOPS|SRE|SYSADMIN|CLOUD|KUBERNETES|DOCKER|LINUX|PYTHON|GOLANG|RUST|JAVA|TYPESCRIPT|"
    r"SQL|POSTGRESQL|TELECOM\w*|5G|VIRTUALISATION|VMWARE|PROXMOX|PARE\s*-?\s*FEU|FIREWALL|"
    r"IA|GENAI|LLMS?|NLP|BIG\s+DATA|MACHINE\s+LEARNING|DEEP\s+LEARNING|INTELLIGENCE\s+ARTIFICIELLE|"
    r"API\s+REST)\b|"
    r"\bDEVELOPP(?:EUR|EUSE)\b|"
    r"\bDEVELOPPEMENT\s+(?:WEB|LOGICIEL|INFORMATIQUE|APPLICATIF|FULL\s*-?\s*STACK|BACK\s*-?\s*END|FRONT\s*-?\s*END|PYTHON|JAVA|RUST)\b|"
    r"\bADMINISTRATEUR\s+(?:SYSTEMES?|RESEAUX?|LINUX|CLOUD|BASES?\s+DE\s+DONNEES?|SECURITE)\b|"
    r"\bINGENIEUR\s+(?:INFORMATIQUE|LOGICIEL|SYSTEMES?|RESEAUX?|CLOUD|CYBER\w*|IA|DATA|DEVOPS|SECURITE\s+INFORMATIQUE|TELECOM\w*)\b|"
    r"\bRESEAUX?\s+(?:INFORMATIQUE|IP|TELECOM\w*|SECURIS\w*)\b|"
    r"\bSYSTEMES?\s+(?:ET\s+RESEAUX?|D\s*'?INFORMATION|LINUX|CLOUD|DISTRIBUES?|SECURISES?)\b|"
    r"\bDATA\s+(?:SCIENTIST|ENGINEER|ANALYST|IA)\b|"
    r"\bAI\s+(?:ENGINEER|RESEARCH|AGENT|SECURITY|MODEL)\b|"
    r"(?<!\w)C\+\+(?!\w)"
    r")"
)

OFFER_EXCLUDE_KEYWORDS = (
    "BOULANGER", "CUISIN", "CHAUFFEUR", "PLOMBIER", "MENUIS", "COIFFEUR",
    "CARISTE", "MAGASINIER", "VENDEUR", "CAISSIER", "MACON", "PEINTRE",
    "ASSISTANTE MATERNELLE", "SOIGNANT", "INFIRMIER", "SERVEUR RESTAURANT",
    "DENTAIRE", "OPTICIEN", "CRECHE", "AUXILIAIRE DE", "RESTAURATION",
    "HOTELLERIE", "EQUIPIER POLYVALENT", "EQUIPER POLYVALENT", "CONSEILLER DE VENTE",
    "PROSPECTION COMMERCIAL", "DEVELOPPEMENT COMMERCIAL", "CONTROLEUR DE GESTION",
    "COMPTABLE", "ASSISTANT ADV", "FROID INDUSTRIEL", "MONTEUR ELECTRICIEN",
    "GENIE CIVIL", "ENROBES", "LEASING AUTOMOBILE", "CHARGE DE COMMUNICATION",
    "CHARPENTE",
)


def _norm(txt: str | None) -> str:
    s = unicodedata.normalize("NFKD", txt or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " " + " ".join(s.upper().split()) + " "


def _clean_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<(br|p|li|h[1-6]|div)\b[^>]*>", "\n", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _detect_zone_and_trajet(
    lon: float | None,
    lat: float | None,
    location_text: str,
    zones: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    if lon is not None and lat is not None:
        for zcode, zdata in zones.items():
            tr = classify_travel_time(lon, lat, zdata["iso20_geojson"], zdata["iso30_geojson"])
            if tr:
                return zcode, tr

    loc_norm = _norm(location_text)
    for zcode, kws in ZONE_CITIES.items():
        for kw in kws:
            if f" {kw} " in loc_norm or kw in loc_norm:
                main_city = kws[0] in loc_norm or kws[1] in loc_norm
                return zcode, "<=20min" if main_city else "20-30min"
    return None, None


def _classify_contract(title: str, raw_types: str = "") -> str | None:
    alt_keys = ("ALTERNAN", "APPRENTI", "CONTRAT PRO", "PROFESSIONNALISATION")
    stage_keys = ("STAGE", "STAGIAIRE", "INTERNSHIP", " INTERN ")

    t_title = _norm(title)
    title_alt = any(k in t_title for k in alt_keys)
    title_stage = any(k in t_title for k in stage_keys)
    if title_alt and not title_stage:
        return "alternance"
    if title_stage and not title_alt:
        return "stage"
    if title_alt and title_stage:
        return "alternance"

    t_all = _norm(f"{title} {raw_types}")
    if any(k in t_all for k in alt_keys):
        return "alternance"
    if any(k in t_all for k in stage_keys):
        return "stage"
    return None


def _is_relevant_domain(title: str, desc: str) -> bool:
    t = _norm(f"{title} {desc[:600]}")
    if any(bad in t for bad in OFFER_EXCLUDE_KEYWORDS):
        return False
    return bool(IT_RELEVANCE_RE.search(t))


_FRENCH_NUMS: dict[str, float] = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6,
    "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12,
}
_MONTH_MAP: dict[str, int] = {
    "janvier": 1, "janv": 1, "fevrier": 2, "fev": 2, "mars": 3, "avril": 4, "avr": 4,
    "mai": 5, "juin": 6, "juillet": 7, "juil": 7, "aout": 8,
    "septembre": 9, "sept": 9, "octobre": 10, "oct": 10, "novembre": 11, "nov": 11,
    "decembre": 12, "dec": 12,
}
_NUM_RE = r"(?:\b\d+(?:[.,]\d+)?\b|\b(?:un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|onze|douze)\b)"
_UNIT_RE = r"(semaines?|sem\.?|mois)(?![a-z])"
_EXPLICIT_DURATION_RE = re.compile(
    rf"(?:duree(?:\s+du\s+(?:contrat|stage)|\s+souhaitee)?(?:\s+est)?\s*(?::|de|d')?|"
    rf"pour\s+une\s+duree\s+(?:de|d')|stage\s+(?:d'une\s+duree\s+(?:de|d')|de|d'))\s*"
    rf"(?:environ\s+)?({_NUM_RE})(?:\s*(?:a|-|ou|/)\s*({_NUM_RE}))?\s*{_UNIT_RE}"
)
_GENERIC_DURATION_RE = re.compile(rf"({_NUM_RE})(?:\s*(?:a|-|ou|/)\s*({_NUM_RE}))?\s*{_UNIT_RE}")
_SKIP_DURATION_SENTENCE_RE = re.compile(
    r"\b(?:livrable|etat de l'art|preavis|periode d'essai|experience|anciennete|conges|delai)\b"
)

_MONTHS_RE = "|".join(sorted(_MONTH_MAP.keys(), key=len, reverse=True))
STAGE_MIN_WEEKS_FLOOR = 10
STAGE_MAX_WEEKS_CAP = 12
STAGE_END_MAX_DATE = date(2027, 8, 20)

_DATE_EXPR_RE = (
    rf"(?:(debut|mi|fin)\s*(?:-|d'|de\s+|du\s+)?)?"
    rf"(?:(\d{{1,2}})(?:er)?\s+)?\b({_MONTHS_RE})\b(?:\s+(202[5-9]|2030))?"
)
_NUM_DATE_EXPR_RE = r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?!\s*(?:sem|mois))"
_START_TRIGGER_RE = (
    r"(?:\ba\s+partir\s+(?:du\s+|de\s+|d'\s*)|"
    r"\ba\s+compter\s+(?:du\s+|de\s+|d'\s*)|"
    r"\bdes\s+(?:(?:le|la)\s+|l'\s*)?(?!lors\b|que\b)|"
    r"\bdebut(?:\s+du\s+stage|\s+(?:souhaite|prevu)(?:\s+(?:le|en|au))?)?"
    r"(?:\s*:\s*|\s+(?:du|le|en|au)\s+|\s+(?=(?:debut|mi|fin|\d|" + _MONTHS_RE + r")))|"
    r"\bdemarrage(?:\s+du\s+stage|\s+prevu)?(?:\s*:\s*|\s+(?:du|le|en|au)\s+|\s+)|"
    r"\bprise\s+de\s+poste(?:\s*:\s*|\s+(?:du|le|en|au)\s+|\s+)|"
    r"(?:poste\s+|stage\s+)?\ba?\s*pourvoir\s+(?:a\s+partir\s+(?:du\s+|de\s+|d'\s*)|a\s+compter\s+(?:du\s+|de\s+|d'\s*)|des\s+(?:(?:le|la)\s+|l'\s*)?|(?:le|en|de)\s+|d'\s*)?|"
    r"\bcommenc(?:ant|e)\s+(?:a\s+partir\s+(?:du\s+|de\s+|d'\s*)|des\s+(?:le\s+)?|(?:le|en)\s+)?)"
)
_FLEX_WINDOW_RE = re.compile(
    rf"(?:pour\s+un\s+)?(?:debut(?:\s+du\s+stage)?|demarrage|prise\s+de\s+poste)\s+(?:prevu(?:e)?\s+)?"
    rf"entre\s+(?:le\s+)?{_DATE_EXPR_RE}\s+et\s+(?:le\s+)?{_DATE_EXPR_RE}"
)
_RANGE_NUM_RE = re.compile(rf"\b(?:du|de)\s+(?:le\s+)?{_NUM_DATE_EXPR_RE}\s+(?:au|a)\s+(?:le\s+)?{_NUM_DATE_EXPR_RE}")
_RANGE_TEXT_RE = re.compile(rf"\b(?:du|de)\s+(?:le\s+)?{_DATE_EXPR_RE}\s+(?:au|a)\s+(?:le\s+|au\s+)?{_DATE_EXPR_RE}")
_TRIGGER_NUM_DATE_RE = re.compile(rf"{_START_TRIGGER_RE}(?:le\s+)?{_NUM_DATE_EXPR_RE}")
_TRIGGER_TEXT_DATE_RE = re.compile(rf"{_START_TRIGGER_RE}(?:le\s+|au\s+|en\s+)?{_DATE_EXPR_RE}")
_PERIOD_RANGE_RE = re.compile(
    rf"\bentre\s+(?:le\s+)?{_DATE_EXPR_RE}\s+et\s+(?:le\s+)?{_DATE_EXPR_RE}"
)
_END_TRIGGER_RE = (
    r"(?:\bjusqu'?\s*(?:au|a|en)\s+(?:(?:le|la)\s+)?|"
    r"\bfin(?:\s+du\s+stage|\s+prevue)?(?:\s*:\s*|\s+(?:au|le|en)\s+)|"
    r"\bse\s+termin(?:ant|e)\s+(?:au|le|en)\s+|"
    r"\bcloture\s+(?:au|le|en)\s+)"
)
_END_TRIGGER_NUM_RE = re.compile(rf"{_END_TRIGGER_RE}(?:le\s+)?{_NUM_DATE_EXPR_RE}")
_END_TRIGGER_TEXT_RE = re.compile(rf"{_END_TRIGGER_RE}(?:le\s+|au\s+|en\s+)?{_DATE_EXPR_RE}")
_END_RENTREE_RE = re.compile(r"\bjusqu'?\s*(?:a|en)\s+(?:la\s+)?rentree\b")
_SUMMER_RE = re.compile(
    r"(?:stage\s+(?:d'|de\s+l')?ete|pour\s+l'ete|cet\s+ete|\bete\s+(202[6-9])\b|periode\s+estivale)"
)


def _norm_stage_text(raw: str | None) -> str:
    s = unicodedata.normalize("NFKD", raw or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("\xa0", " ").replace("–", "-").replace("—", "-").replace("−", "-")
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"[ \t]+", " ", s.lower())
    return re.sub(r" *\n *", "\n", s).strip()


def _parse_num_token(tok: str | None) -> float | None:
    if not tok:
        return None
    t = tok.strip().replace(",", ".")
    if t in _FRENCH_NUMS:
        return _FRENCH_NUMS[t]
    try:
        return float(t)
    except ValueError:
        return None


def _convert_duration_to_weeks(raw1: str, raw2: str | None, unit: str) -> tuple[int, int] | None:
    v1 = _parse_num_token(raw1)
    v2 = _parse_num_token(raw2) if raw2 else v1
    if v1 is None or v2 is None or v1 > v2 or v1 <= 0:
        return None
    if unit.startswith("sem"):
        w1, w2 = round(v1), round(v2)
        return (w1, w2) if 1 <= w1 <= w2 <= 60 else None
    if unit == "mois" and 1 <= v1 <= v2 <= 36:
        w1, w2 = int(v1 * 4), round(v2 * 4.33)
        return (w1, max(w1, w2))
    return None


def _parse_duration_weeks(norm_desc: str, norm_title: str) -> tuple[int | None, int | None]:
    combined = f"{norm_title}\n{norm_desc}"
    m = _EXPLICIT_DURATION_RE.search(combined)
    if m:
        res = _convert_duration_to_weeks(m.group(1), m.group(2), m.group(3))
        if res:
            return res

    mt = _GENERIC_DURATION_RE.search(norm_title)
    if mt:
        res = _convert_duration_to_weeks(mt.group(1), mt.group(2), mt.group(3))
        if res:
            return res

    for sentence in re.split(r"[\n.;]+", norm_desc):
        if not sentence or _SKIP_DURATION_SENTENCE_RE.search(sentence):
            continue
        ms = _GENERIC_DURATION_RE.search(sentence)
        if ms:
            res = _convert_duration_to_weeks(ms.group(1), ms.group(2), ms.group(3))
            if res:
                return res
    return None, None


def _resolve_date(day: int, month: int, year_opt: int | None, ref_date: date) -> date | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if year_opt is not None and 2025 <= year_opt <= 2030:
        max_d = calendar.monthrange(year_opt, month)[1]
        return date(year_opt, month, min(day, max_d))
    year = ref_date.year
    max_d = calendar.monthrange(year, month)[1]
    cand = date(year, month, min(day, max_d))
    if cand < ref_date - timedelta(days=35):
        year += 1
        max_d = calendar.monthrange(year, month)[1]
        cand = date(year, month, min(day, max_d))
    return cand


def _parse_numeric_date_groups(d_s: str, m_s: str, y_raw: str | None, ref_date: date) -> date | None:
    y_opt = (2000 + int(y_raw)) if (y_raw and len(y_raw) == 2) else (int(y_raw) if y_raw else None)
    return _resolve_date(int(d_s), int(m_s), y_opt, ref_date)


def _date_from_text_groups(qual: str | None, day_s: str | None, month_s: str, year_s: str | None, ref_date: date) -> date | None:
    month = _MONTH_MAP.get(month_s)
    if not month:
        return None
    if day_s:
        day = int(day_s)
    elif qual == "fin":
        day = 25 if month == 2 else 27
    elif qual == "mi":
        day = 15
    else:
        day = 1
    year_opt = int(year_s) if year_s else None
    return _resolve_date(day, month, year_opt, ref_date)


def _date_from_end_text_groups(
    qual: str | None,
    day_s: str | None,
    month_s: str,
    year_s: str | None,
    ref_date: date,
    start_dt: date | None = None,
) -> date | None:
    month = _MONTH_MAP.get(month_s)
    if not month:
        return None
    year_opt = int(year_s) if year_s else None
    if year_opt is None and start_dt is not None:
        year_opt = start_dt.year if month >= start_dt.month else start_dt.year + 1
    if day_s:
        day = int(day_s)
    elif qual == "debut":
        day = 1
    elif qual == "fin":
        base = _resolve_date(1, month, year_opt, ref_date)
        if not base:
            return None
        day = calendar.monthrange(base.year, month)[1]
    else:
        day = 15
    return _resolve_date(day, month, year_opt, ref_date)


def _infer_pair_start_year(m1: int | None, m2: int | None, y1_s: str | None, y2_s: str | None) -> str | None:
    if y1_s or not (y2_s and m1 and m2):
        return y1_s
    return str(int(y2_s) if m1 <= m2 else int(y2_s) - 1)


def _strip_pub_metadata(text: str) -> str:
    cleaned = re.sub(
        r"date\s+de\s+publication\s*:\s*(?:[a-z]+\s+\d{1,2},?\s+\d{4}(?:,?\s*\d{1,2}:\d{2}\s*(?:am|pm)?)?|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}\s+[a-z]+\s+\d{4}|[^\n]{0,28})",
        " ",
        text,
    )
    return re.sub(
        r"publi[e]e?\s+le\s*:?\s*(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}\s+[a-z]+\s+\d{4}|[^\n]{0,20})",
        " ",
        cleaned,
    )


def _parse_stage_start_date(norm_desc: str, norm_title: str, ref_date: date) -> date | None:
    clean_text = _strip_pub_metadata(f"{norm_title}\n{norm_desc}")

    mw = _FLEX_WINDOW_RE.search(clean_text)
    if mw:
        # Upper bound reflects the latest flexible start the employer accepts.
        return _date_from_text_groups(mw.group(5), mw.group(6), mw.group(7), mw.group(8), ref_date)

    mrn = _RANGE_NUM_RE.search(clean_text)
    if mrn:
        y1_s = _infer_pair_start_year(int(mrn.group(2)), int(mrn.group(5)), mrn.group(3), mrn.group(6))
        res = _parse_numeric_date_groups(mrn.group(1), mrn.group(2), y1_s, ref_date)
        if res:
            return res

    mrt = _RANGE_TEXT_RE.search(clean_text)
    if mrt:
        y1_s = _infer_pair_start_year(_MONTH_MAP.get(mrt.group(3)), _MONTH_MAP.get(mrt.group(7)), mrt.group(4), mrt.group(8))
        return _date_from_text_groups(mrt.group(1), mrt.group(2), mrt.group(3), y1_s, ref_date)

    mn = _TRIGGER_NUM_DATE_RE.search(clean_text)
    if mn:
        res = _parse_numeric_date_groups(mn.group(1), mn.group(2), mn.group(3), ref_date)
        if res:
            return res

    mt = _TRIGGER_TEXT_DATE_RE.search(clean_text)
    if mt:
        return _date_from_text_groups(mt.group(1), mt.group(2), mt.group(3), mt.group(4), ref_date)

    mp = _PERIOD_RANGE_RE.search(clean_text)
    if mp:
        y1_s = _infer_pair_start_year(_MONTH_MAP.get(mp.group(3)), _MONTH_MAP.get(mp.group(7)), mp.group(4), mp.group(8))
        return _date_from_text_groups(mp.group(1), mp.group(2), mp.group(3), y1_s, ref_date)

    ms = _SUMMER_RE.search(clean_text)
    if ms:
        y_opt = int(ms.group(1)) if ms.group(1) else None
        return _resolve_date(1, 6, y_opt, ref_date)
    return None


def _parse_stage_end_date(
    norm_desc: str,
    norm_title: str,
    ref_date: date,
    start_dt: date | None,
) -> date | None:
    clean_text = _strip_pub_metadata(f"{norm_title}\n{norm_desc}")
    clean_text = _FLEX_WINDOW_RE.sub(" ", clean_text)

    mrn = _RANGE_NUM_RE.search(clean_text)
    if mrn:
        res = _parse_numeric_date_groups(mrn.group(4), mrn.group(5), mrn.group(6), ref_date)
        if res:
            return res

    mrt = _RANGE_TEXT_RE.search(clean_text)
    if mrt:
        return _date_from_end_text_groups(mrt.group(5), mrt.group(6), mrt.group(7), mrt.group(8), ref_date, start_dt)

    mp = _PERIOD_RANGE_RE.search(clean_text)
    if mp:
        return _date_from_end_text_groups(mp.group(5), mp.group(6), mp.group(7), mp.group(8), ref_date, start_dt)

    men = _END_TRIGGER_NUM_RE.search(clean_text)
    if men:
        res = _parse_numeric_date_groups(men.group(1), men.group(2), men.group(3), ref_date)
        if res:
            return res

    met = _END_TRIGGER_TEXT_RE.search(clean_text)
    if met:
        return _date_from_end_text_groups(met.group(1), met.group(2), met.group(3), met.group(4), ref_date, start_dt)

    if _END_RENTREE_RE.search(clean_text):
        return _resolve_date(1, 9, None, ref_date)
    return None


def extract_stage_timing(
    title: str,
    description: str | None,
    date_publication: date | None = None,
) -> tuple[int | None, int | None, date | None, date | None]:
    """Extract normalized stage duration range (weeks), start date, and end date."""
    norm_title = _norm_stage_text(title)
    norm_desc = _norm_stage_text(description)
    ref_date = date_publication or date.today()
    dmin, dmax = _parse_duration_weeks(norm_desc, norm_title)
    start_dt = _parse_stage_start_date(norm_desc, norm_title, ref_date)
    end_dt = _parse_stage_end_date(norm_desc, norm_title, ref_date, start_dt)
    return dmin, dmax, start_dt, end_dt


def is_stage_timing_acceptable(
    dmin: int | None,
    dmax: int | None,
    start_dt: date | None,
    end_dt: date | None,
) -> bool:
    """Reject stages > 12 weeks or ending after mid-August (rentree cutoff)."""
    if dmin is not None and dmin > STAGE_MAX_WEEKS_CAP:
        return False
    if end_dt is not None and end_dt > STAGE_END_MAX_DATE:
        return False
    if start_dt is not None:
        implied_end = start_dt + timedelta(weeks=dmin or STAGE_MIN_WEEKS_FLOOR)
        if implied_end > STAGE_END_MAX_DATE:
            return False
    return True


def _match_company(cur, siret: str | None, name: str) -> tuple[int | None, str | None, str | None]:
    if siret and len(siret.strip()) == 14:
        cur.execute("SELECT id, description, trajet_tranche FROM entreprises WHERE siret = %s LIMIT 1", (siret.strip(),))
        r = cur.fetchone()
        if r:
            return r["id"], r["description"], r["trajet_tranche"]

    clean = re.sub(r"[^A-Z0-9 ]+", " ", _norm(name)).strip()
    if len(clean) >= 3:
        first_tok = clean.split()[0]
        if len(first_tok) >= 4:
            cur.execute(
                "SELECT id, nom, description, trajet_tranche FROM entreprises WHERE UPPER(nom) LIKE %s ORDER BY priorite ASC LIMIT 5",
                (f"%{first_tok}%",),
            )
            for cand in cur.fetchall():
                cnorm = _norm(cand["nom"])
                if first_tok in cnorm:
                    return cand["id"], cand["description"], cand["trajet_tranche"]
    return None, None, None


def _upsert_offer(cur, item: dict[str, Any]) -> bool | None:
    dedup_raw = f"{_norm(item['entreprise_nom']).strip()}|{_norm(item['titre']).strip()}|{item['zone']}"
    item["dedup_hash"] = hashlib.sha256(dedup_raw.encode("utf-8")).hexdigest()

    if item.get("type_contrat") == "stage":
        dmin, dmax, start_dt, end_dt = extract_stage_timing(
            item["titre"],
            item.get("description"),
            item.get("date_publication"),
        )
        if not is_stage_timing_acceptable(dmin, dmax, start_dt, end_dt):
            return None
        item["duree_semaines_min"] = dmin
        item["duree_semaines_max"] = dmax
        item["date_debut_stage"] = start_dt
        item["date_fin_stage"] = end_dt
    else:
        item["duree_semaines_min"] = None
        item["duree_semaines_max"] = None
        item["date_debut_stage"] = None
        item["date_fin_stage"] = None

    ent_id, ent_desc, ent_trajet = _match_company(cur, item.pop("siret", None), item["entreprise_nom"])
    item["entreprise_id"] = ent_id
    if not item.get("entreprise_resume") and ent_desc:
        item["entreprise_resume"] = ent_desc
    if not item.get("trajet_tranche") and ent_trajet:
        item["trajet_tranche"] = ent_trajet

    cur.execute(
        """
        INSERT INTO offres (
            source, external_id, dedup_hash, entreprise_id, entreprise_nom, entreprise_resume,
            titre, type_contrat, duree_semaines_min, duree_semaines_max, date_debut_stage, date_fin_stage,
            zone, ville, trajet_tranche, url_directe, description, date_publication
        )
        VALUES (
            %(source)s, %(external_id)s, %(dedup_hash)s, %(entreprise_id)s, %(entreprise_nom)s, %(entreprise_resume)s,
            %(titre)s, %(type_contrat)s, %(duree_semaines_min)s, %(duree_semaines_max)s, %(date_debut_stage)s, %(date_fin_stage)s,
            %(zone)s, %(ville)s, %(trajet_tranche)s, %(url_directe)s, %(description)s, %(date_publication)s
        )
        ON CONFLICT (source, external_id) DO UPDATE SET
            entreprise_id = COALESCE(offres.entreprise_id, EXCLUDED.entreprise_id),
            entreprise_resume = COALESCE(NULLIF(offres.entreprise_resume, ''), EXCLUDED.entreprise_resume),
            duree_semaines_min = COALESCE(offres.duree_semaines_min, EXCLUDED.duree_semaines_min),
            duree_semaines_max = COALESCE(offres.duree_semaines_max, EXCLUDED.duree_semaines_max),
            date_debut_stage = COALESCE(offres.date_debut_stage, EXCLUDED.date_debut_stage),
            date_fin_stage = COALESCE(offres.date_fin_stage, EXCLUDED.date_fin_stage),
            trajet_tranche = COALESCE(offres.trajet_tranche, EXCLUDED.trajet_tranche),
            url_directe = EXCLUDED.url_directe,
            description = COALESCE(NULLIF(EXCLUDED.description, ''), offres.description),
            updated_at = NOW()
        RETURNING (xmax = 0) AS inserted
        """,
        item,
    )
    row = cur.fetchone()
    return bool(row and row["inserted"])


def _parse_date(val: str | None) -> date | None:
    if not val:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(val).strip())
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None
    return None


def _collect_lba(client: httpx.Client, zones: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    token = os.environ.get("LBA_API_TOKEN", "").strip()
    if not token:
        return []
    out: list[dict[str, Any]] = []
    for zcode, cfg in ZONES.items():
        params = {
            "latitude": cfg["lat"],
            "longitude": cfg["lon"],
            "radius": 35,
            "romes": "M1805,M1802,M1801,M1803,M1804,M1806,M1810,I1401",
            "caller": "homelab_career_radar",
        }
        resp = client.get(
            LBA_SEARCH_URL,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        for job in resp.json().get("jobs") or []:
            ident = job.get("identifier") or {}
            wp = job.get("workplace") or {}
            apply = job.get("apply") or {}
            contract = job.get("contract") or {}
            offer = job.get("offer") or {}
            loc = wp.get("location") or {}
            coords = (loc.get("geopoint") or {}).get("coordinates") or []
            lon = float(coords[0]) if len(coords) == 2 else None
            lat = float(coords[1]) if len(coords) == 2 else None
            addr = loc.get("address") or ""

            matched_zone, trajet = _detect_zone_and_trajet(lon, lat, addr, zones)
            if not matched_zone:
                continue

            titre = (offer.get("title") or "").strip()
            desc = (offer.get("description") or "").strip()
            if not titre or not _is_relevant_domain(titre, desc):
                continue

            url = (apply.get("url") or "").strip()
            if not url:
                continue

            ext_id = str(ident.get("id") or ident.get("partner_job_id") or hashlib.md5(url.encode()).hexdigest())
            ctypes = " ".join(contract.get("type") or [])
            tcontrat = _classify_contract(titre, ctypes) or "alternance"
            emp_name = (wp.get("brand") or wp.get("legal_name") or wp.get("name") or "Entreprise").strip()
            pub_info = offer.get("publication") or {}

            out.append({
                "source": f"lba:{ident.get('partner_label') or 'lba'}"[:40],
                "external_id": ext_id,
                "siret": wp.get("siret"),
                "entreprise_nom": emp_name,
                "entreprise_resume": (wp.get("description") or "").strip() or None,
                "titre": titre,
                "type_contrat": tcontrat,
                "zone": matched_zone,
                "ville": addr,
                "trajet_tranche": trajet,
                "url_directe": url,
                "description": desc,
                "date_publication": _parse_date(pub_info.get("creation")),
            })
    return out


def _collect_thales(client: httpx.Client, zones: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for query in ("Brest", "Lannion", "Caen"):
        time.sleep(1.2)
        resp = client.post(
            THALES_JOBS_URL,
            json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query},
        )
        resp.raise_for_status()
        for post in resp.json().get("jobPostings") or []:
            path = post.get("externalPath") or ""
            title = (post.get("title") or "").strip()
            if not path or path in seen_paths or not title:
                continue
            loc_text = f"{post.get('locationsText') or ''} {title}"
            tcontrat = _classify_contract(title)
            if not tcontrat:
                continue

            detail = {}
            try:
                time.sleep(1.2)
                dr = client.get(f"{THALES_BASE_URL}/wday/cxs/thales/Careers{path}")
                if dr.status_code == 200:
                    detail = dr.json().get("jobPostingInfo") or {}
            except Exception:
                pass

            full_loc = f"{detail.get('location') or ''} {post.get('locationsText') or ''} {title}"
            zcode, trajet = _detect_zone_and_trajet(None, None, full_loc, zones)
            if not zcode:
                continue
            desc = _clean_html(detail.get("jobDescription") or "")
            if not _is_relevant_domain(title, desc):
                continue

            seen_paths.add(path)
            out.append({
                "source": "thales_workday",
                "external_id": path,
                "siret": None,
                "entreprise_nom": "Thales",
                "entreprise_resume": None,
                "titre": title,
                "type_contrat": tcontrat,
                "zone": zcode,
                "ville": detail.get("location") or post.get("locationsText") or query,
                "trajet_tranche": trajet,
                "url_directe": f"{THALES_BASE_URL}/fr-FR/Careers{path}",
                "description": desc,
                "date_publication": _parse_date(detail.get("startDate")),
            })
    return out


def _collect_sopra(client: httpx.Client, zones: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for query in ("Brest", "Guipavas", "Lannion", "Caen"):
        resp = client.get(SOPRA_JOBS_URL, params={"q": query, "limit": 50})
        resp.raise_for_status()
        for post in resp.json().get("content") or []:
            pid = str(post.get("id") or "")
            title = (post.get("name") or "").strip()
            if not pid or pid in seen_ids or not title:
                continue
            emp_type = str((post.get("typeOfEmployment") or {}).get("id") or "")
            exp_lvl = str((post.get("experienceLevel") or {}).get("id") or "")
            tcontrat = _classify_contract(title, f"{emp_type} {exp_lvl}")
            if not tcontrat:
                continue

            loc = post.get("location") or {}
            try:
                lat = float(loc.get("latitude")) if loc.get("latitude") else None
                lon = float(loc.get("longitude")) if loc.get("longitude") else None
            except (TypeError, ValueError):
                lat = lon = None
            city = loc.get("city") or ""
            zcode, trajet = _detect_zone_and_trajet(lon, lat, f"{city} {loc.get('fullLocation') or ''}", zones)
            if not zcode:
                continue

            detail = {}
            try:
                dr = client.get(f"{SOPRA_JOBS_URL}/{pid}")
                if dr.status_code == 200:
                    detail = dr.json()
            except Exception:
                pass
            secs = (detail.get("jobAd") or {}).get("sections") or {}
            desc_html = (secs.get("jobDescription") or {}).get("text", "") + "\n" + (secs.get("qualifications") or {}).get("text", "")
            desc = _clean_html(desc_html)
            if not _is_relevant_domain(title, desc):
                continue

            seen_ids.add(pid)
            url = (detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/SopraSteria1/{pid}").strip()
            out.append({
                "source": "sopra_smartrecruiters",
                "external_id": pid,
                "siret": None,
                "entreprise_nom": "Sopra Steria",
                "entreprise_resume": None,
                "titre": title,
                "type_contrat": tcontrat,
                "zone": zcode,
                "ville": city or query,
                "trajet_tranche": trajet,
                "url_directe": url,
                "description": desc,
                "date_publication": _parse_date(post.get("releasedDate")),
            })
    return out


def _collect_nokia(client: httpx.Client, zones: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for query in ("Lannion", "Brest", "Caen"):
        finder = f"findReqs;siteNumber=CX_1,keyword={query},limit=25"
        resp = client.get(
            NOKIA_JOBS_URL,
            params={"onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": finder},
        )
        resp.raise_for_status()
        reqs = (resp.json().get("items") or [{}])[0].get("requisitionList") or []
        for req in reqs:
            jid = str(req.get("Id") or "")
            title = (req.get("Title") or "").strip()
            if not jid or jid in seen_ids or not title:
                continue
            tcontrat = _classify_contract(title, str(req.get("ContractType") or ""))
            if not tcontrat:
                continue
            loc_str = f"{req.get('PrimaryLocation') or ''} {req.get('PrimaryLocationCountry') or ''} {title}"
            zcode, trajet = _detect_zone_and_trajet(None, None, loc_str, zones)
            if not zcode:
                continue
            desc = _clean_html(req.get("ShortDescriptionStr") or "")
            if not _is_relevant_domain(title, desc):
                continue
            seen_ids.add(jid)
            out.append({
                "source": "nokia_hcm",
                "external_id": jid,
                "siret": None,
                "entreprise_nom": "Nokia",
                "entreprise_resume": None,
                "titre": title,
                "type_contrat": tcontrat,
                "zone": zcode,
                "ville": req.get("PrimaryLocation") or query,
                "trajet_tranche": trajet,
                "url_directe": f"https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/{jid}",
                "description": desc,
                "date_publication": _parse_date(req.get("PostedDate")),
            })
    return out


def _collect_arkea(client: httpx.Client, zones: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    resp = client.get(ARKEA_RSS_URL, headers={"Accept": "application/rss+xml, application/xml, text/xml"})
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = _clean_html(item.findtext("description") or "")
        cats = [c.text.strip() for c in item.findall("category") if c.text]
        cat_txt = " ".join(cats)
        if not title or not link:
            continue
        tcontrat = _classify_contract(title, cat_txt)
        if not tcontrat:
            continue
        zcode, trajet = _detect_zone_and_trajet(None, None, f"{cat_txt} {desc[:300]}", zones)
        if not zcode:
            continue
        if not _is_relevant_domain(title, f"{cat_txt} {desc}"):
            continue
        ext_id = hashlib.md5(link.encode("utf-8")).hexdigest()
        city = next((c for c in cats if any(k in _norm(c) for k in ("BREST", "RELECQ", "LOPERHET", "LANNION", "CAEN", "LANDERNEAU"))), "Brest / Relecq-Kerhuon")
        out.append({
            "source": "arkea_rss",
            "external_id": ext_id,
            "siret": None,
            "entreprise_nom": "Crédit Mutuel Arkéa",
            "entreprise_resume": None,
            "titre": title,
            "type_contrat": tcontrat,
            "zone": zcode,
            "ville": city,
            "trajet_tranche": trajet,
            "url_directe": link,
            "description": desc,
            "date_publication": None,
        })
    return out


def _collect_adzuna(client: httpx.Client, zones: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    city_map = {"loperhet": "Brest", "lannion": "Lannion", "caen": "Caen"}
    what_or = "cybersecurite cyber informatique linux cloud devops python data ia reseaux systemes developpeur logiciel"
    for zcode, city in city_map.items():
        for contract_kw in ("stage", "alternance"):
            resp = client.get(
                "https://api.adzuna.com/v1/api/jobs/fr/search/1",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "results_per_page": 50,
                    "category": "it-jobs",
                    "what": contract_kw,
                    "what_or": what_or,
                    "where": city,
                    "distance": 30,
                    "max_days_old": 30,
                },
            )
            resp.raise_for_status()
            for r in resp.json().get("results") or []:
                aid = str(r.get("id") or "")
                title = (r.get("title") or "").strip()
                url = (r.get("redirect_url") or "").strip()
                if not aid or aid in seen_ids or not title or not url:
                    continue
                desc = _clean_html(r.get("description") or "")
                tcontrat = _classify_contract(title, desc[:250])
                if not tcontrat:
                    continue
                if not _is_relevant_domain(title, desc):
                    continue
                lat = r.get("latitude")
                lon = r.get("longitude")
                loc_disp = ((r.get("location") or {}).get("display_name") or city)
                if lat is not None and lon is not None:
                    mz, trajet = _detect_zone_and_trajet(float(lon), float(lat), loc_disp, zones)
                    if not mz:
                        continue
                else:
                    mz, trajet = _detect_zone_and_trajet(None, None, loc_disp, zones)
                    if not mz:
                        mz, trajet = zcode, "20-30min"
                seen_ids.add(aid)
                out.append({
                    "source": "adzuna",
                    "external_id": aid,
                    "siret": None,
                    "entreprise_nom": ((r.get("company") or {}).get("display_name") or "Entreprise").strip(),
                    "entreprise_resume": None,
                    "titre": title,
                    "type_contrat": tcontrat,
                    "zone": mz,
                    "ville": loc_disp,
                    "trajet_tranche": trajet,
                    "url_directe": url,
                    "description": desc,
                    "date_publication": _parse_date(r.get("created")),
                })
    return out


def collect_all_offers(cur) -> dict[str, Any]:
    """Run all configured offer collectors and upsert normalized rows into `offres`."""
    zones = ensure_zones_isochrones(cur, force_refresh=False)
    collectors = [
        ("lba", _collect_lba),
        ("thales_workday", _collect_thales),
        ("sopra_smartrecruiters", _collect_sopra),
        ("nokia_hcm", _collect_nokia),
        ("arkea_rss", _collect_arkea),
        ("adzuna", _collect_adzuna),
    ]

    summary: dict[str, Any] = {"sources": {}, "total_fetched": 0, "total_inserted": 0, "total_upserted": 0}
    with httpx.Client(timeout=25.0, follow_redirects=True, headers={"User-Agent": "Homelab-Career-Radar/1.0"}) as client:
        for name, fn in collectors:
            try:
                items = fn(client, zones)
                inserted = 0
                upserted = 0
                skipped_timing = 0
                for item in items:
                    res = _upsert_offer(cur, item)
                    if res is None:
                        skipped_timing += 1
                    else:
                        upserted += 1
                        if res:
                            inserted += 1
                summary["sources"][name] = {
                    "fetched": len(items),
                    "inserted": inserted,
                    "skipped_stage_timing": skipped_timing,
                }
                summary["total_fetched"] += len(items)
                summary["total_inserted"] += inserted
                summary["total_upserted"] += upserted
            except Exception as exc:
                logger.warning("Collector %s failed: %s", name, exc)
                summary["sources"][name] = {"error": str(exc)[:220]}

    return summary
