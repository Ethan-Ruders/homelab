"""Multi-source offer collector for Homelab Career Radar (LBA, Thales, Sopra, Nokia, Arkea, Adzuna)."""
from __future__ import annotations

from datetime import date
import hashlib
import html
import logging
import os
import re
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

IT_RELEVANCE_KEYWORDS = (
    "INFORMATIQUE", "CYBER", "SECURITE", "SECURITY", "SOC", "PENTEST", "DEVSECOPS",
    "INTELLIGENCE ARTIFICIELLE", " IA ", " AI ", "MACHINE LEARNING", "DEEP LEARNING", "DATA",
    "SYSTEME", "SYSTEM", "RESEAU", "NETWORK", "LINUX", "CLOUD", "DEVOPS", "INFRASTRUCTURE",
    "DEVELOPP", "SOFTWARE", "LOGICIEL", "PYTHON", "FULLSTACK", "BACKEND", "WEB", "API",
    "ADMIN", "VIRTUALISATION", "TELECOM", "5G", "INGENIEUR", "TECH", "SI ", "S.I.",
)

OFFER_EXCLUDE_KEYWORDS = (
    "BOULANGER", "CUISIN", "CHAUFFEUR", "PLOMBIER", "MENUIS", "COIFFEUR",
    "CARISTE", "MAGASINIER", "VENDEUR", "CAISSIER", "MACON", "PEINTRE",
    "ASSISTANTE MATERNELLE", "SOIGNANT", "INFIRMIER", "SERVEUR RESTAURANT",
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
    t = _norm(f"{title} {raw_types}")
    has_alt = any(k in t for k in ("ALTERNAN", "APPRENTI", "CONTRAT PRO", "PROFESSIONNALISATION"))
    has_stage = any(k in t for k in ("STAGE", "STAGIAIRE", "INTERNSHIP", " INTERN "))
    if has_alt:
        return "alternance"
    if has_stage:
        return "stage"
    return None


def _is_relevant_domain(title: str, desc: str) -> bool:
    t = _norm(f"{title} {desc[:600]}")
    if any(bad in t for bad in OFFER_EXCLUDE_KEYWORDS):
        return False
    return any(kw in t for kw in IT_RELEVANCE_KEYWORDS)


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


def _upsert_offer(cur, item: dict[str, Any]) -> bool:
    dedup_raw = f"{_norm(item['entreprise_nom']).strip()}|{_norm(item['titre']).strip()}|{item['zone']}"
    item["dedup_hash"] = hashlib.sha256(dedup_raw.encode("utf-8")).hexdigest()

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
            titre, type_contrat, zone, ville, trajet_tranche, url_directe, description, date_publication
        )
        VALUES (
            %(source)s, %(external_id)s, %(dedup_hash)s, %(entreprise_id)s, %(entreprise_nom)s, %(entreprise_resume)s,
            %(titre)s, %(type_contrat)s, %(zone)s, %(ville)s, %(trajet_tranche)s, %(url_directe)s, %(description)s, %(date_publication)s
        )
        ON CONFLICT (source, external_id) DO UPDATE SET
            entreprise_id = COALESCE(offres.entreprise_id, EXCLUDED.entreprise_id),
            entreprise_resume = COALESCE(NULLIF(offres.entreprise_resume, ''), EXCLUDED.entreprise_resume),
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
    for zcode, city in city_map.items():
        for what in ("stage cybersecurite ia informatique reseau", "alternance cybersecurite ia informatique reseau"):
            resp = client.get(
                "https://api.adzuna.com/v1/api/jobs/fr/search/1",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "results_per_page": 25,
                    "what": what,
                    "where": city,
                    "distance": 30,
                },
            )
            resp.raise_for_status()
            for r in resp.json().get("results") or []:
                aid = str(r.get("id") or "")
                title = (r.get("title") or "").strip()
                url = (r.get("redirect_url") or "").strip()
                if not aid or aid in seen_ids or not title or not url:
                    continue
                tcontrat = _classify_contract(title, what)
                if not tcontrat:
                    continue
                desc = _clean_html(r.get("description") or "")
                if not _is_relevant_domain(title, desc):
                    continue
                lat = r.get("latitude")
                lon = r.get("longitude")
                loc_disp = ((r.get("location") or {}).get("display_name") or city)
                mz, trajet = _detect_zone_and_trajet(float(lon) if lon else None, float(lat) if lat else None, loc_disp, zones)
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
                for item in items:
                    if _upsert_offer(cur, item):
                        inserted += 1
                summary["sources"][name] = {"fetched": len(items), "inserted": inserted}
                summary["total_fetched"] += len(items)
                summary["total_inserted"] += inserted
                summary["total_upserted"] += len(items)
            except Exception as exc:
                logger.warning("Collector %s failed: %s", name, exc)
                summary["sources"][name] = {"error": str(exc)[:220]}

    return summary
