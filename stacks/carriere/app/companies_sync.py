"""Company directory sync via INSEE/DINUM SIRENE API, IGN car isochrones, and La Bonne Alternance."""
from __future__ import annotations

import logging
import os
import time
import unicodedata
from typing import Any

import httpx

from .geo import ZONES, classify_travel_time, ensure_zones_isochrones, haversine_km

logger = logging.getLogger(__name__)

SIRENE_NEAR_URL = "https://recherche-entreprises.api.gouv.fr/near_point"
LBA_SEARCH_URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"

TRANCHES_GE_10 = "11,12,21,22,31,32,41,42,51,52,53"
VALID_LOCAL_TRANCHES = {"11", "12", "21", "22", "31", "32", "41", "42", "51", "52", "53"}
LARGE_LOCAL_TRANCHES = {"32", "41", "42", "51", "52", "53"}

EFFECTIF_LABELS: dict[str, str] = {
    "00": "0 salarié",
    "01": "1–2 salariés",
    "02": "3–5 salariés",
    "03": "6–9 salariés",
    "11": "10–19 salariés",
    "12": "20–49 salariés",
    "21": "50–99 salariés",
    "22": "100–199 salariés",
    "31": "200–249 salariés",
    "32": "250–499 salariés",
    "41": "500–999 salariés",
    "42": "1 000–1 999 salariés",
    "51": "2 000–4 999 salariés",
    "52": "5 000–9 999 salariés",
    "53": "10 000+ salariés",
    "NN": "Non renseigné",
}

CORE_TECH_NAF: dict[str, tuple[str, list[str]]] = {
    "62.01Z": ("Programmation informatique et développement logiciel", ["Développement", "Logiciel"]),
    "62.02A": ("Conseil en systèmes et logiciels informatiques (ESN / Cyber / Cloud)", ["IT", "Systèmes", "Cyber"]),
    "62.02B": ("Tierce maintenance de systèmes et d'applications informatiques", ["Systèmes", "Réseaux"]),
    "62.03Z": ("Gestion d'installations informatiques (infogérance, infra, cloud)", ["Systèmes", "Réseaux", "Infra"]),
    "62.09Z": ("Autres activités informatiques (expertise technique, intégration)", ["IT", "Systèmes"]),
    "63.11Z": ("Traitement de données, hébergement, datacenter et plateformes cloud/IA", ["Cloud", "IA", "Data"]),
    "63.12Z": ("Portails Internet et plateformes numériques", ["Web", "Plateforme"]),
    "58.29A": ("Édition de logiciels système et de réseau", ["Logiciel", "Systèmes", "Réseaux"]),
    "58.29B": ("Édition de logiciels outils de développement et langages", ["Logiciel", "DevTools"]),
    "58.29C": ("Édition de logiciels applicatifs (SaaS / Cyber / Data)", ["Logiciel", "IA"]),
    "61.10Z": ("Télécommunications filaires (opérateurs & réseaux)", ["Réseaux", "Télécoms"]),
    "61.20Z": ("Télécommunications sans fil (radio, 4G/5G, réseaux)", ["Réseaux", "Télécoms"]),
    "61.90Z": ("Autres activités de télécommunication et réseaux critiques", ["Réseaux", "Télécoms"]),
    "72.19Z": ("R&D en sciences physiques, informatique, IA et sciences de l'ingénieur", ["R&D", "IA", "Cyber"]),
    "26.30Z": ("Fabrication d'équipements de communication et réseaux", ["Télécoms", "Réseaux"]),
    "26.51A": ("Fabrication d'équipements d'aide à la navigation / systèmes défense", ["Défense", "Systèmes"]),
}

MAJOR_TECH_NAF: dict[str, tuple[str, list[str]]] = {
    "71.12B": ("Ingénierie et études techniques (systèmes complexes, naval, défense, IT)", ["Ingénierie", "Systèmes"]),
    "30.11Z": ("Construction navale militaire et systèmes embarqués critiques", ["Défense", "Naval"]),
    "64.19Z": ("Banque et systèmes d'information financiers critiques (SI / Cyber / IA)", ["Finance-IT", "Cyber", "IA"]),
    "84.22Z": ("Défense nationale et organismes techniques du ministère des Armées", ["Défense", "Cyber"]),
    "85.42Z": ("Enseignement supérieur et laboratoires de recherche (IA / Cyber / Réseaux)", ["Recherche", "IA", "Cyber"]),
}

FLAGSHIP_PROFILES: list[tuple[str, dict[str, Any]]] = [
    ("THALES", {
        "tags": ["Cyber", "Défense", "IA", "Systèmes", "Réseaux"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Leader mondial de la défense, cybersécurité, systèmes navals/aéronautiques et IA critique (très présent sur Brest métropole / Caen / Normandie).",
        "site": "https://www.thalesgroup.com/fr",
        "careers": "https://thales.wd3.myworkdayjobs.com/fr-FR/Careers",
    }),
    ("NAVAL GROUP", {
        "tags": ["Défense", "Cyber", "Systèmes", "Réseaux"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Architecte industriel des navires et sous-marins militaires à Brest : cybersécurité des systèmes de combat, réseaux sécurisés, simulation et IA embarquée.",
        "site": "https://www.naval-group.com",
        "careers": "https://www.naval-group.com/fr/nous-rejoindre",
    }),
    ("NOKIA", {
        "tags": ["Réseaux", "Cyber", "Télécoms", "IA"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Pôle stratégique à Lannion hébergeant le Centre d'Excellence Européen en Cybersécurité et R&D réseaux 5G/6G, cloud télécoms et automatisation IA.",
        "site": "https://www.nokia.com",
        "careers": "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
    }),
    ("ORANGE", {
        "tags": ["Cyber", "Réseaux", "Cloud", "IA", "Télécoms"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Opérateur télécom et leader européen cyber via Orange Cyberdefense (R&D Lannion, réseaux critiques, SOC, cloud, IA).",
        "site": "https://www.orange.com",
        "careers": "https://orange.jobs",
    }),
    ("ARKEA", {
        "tags": ["Cyber", "IA", "Data", "Systèmes", "Développement"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Crédit Mutuel Arkéa (siège près de Brest/Relecq-Kerhuon) : gros employeur IT interne en cybersécurité bancaire, fraude IA, cloud et architecture SI.",
        "site": "https://www.arkea.com",
        "careers": "https://cmarkea-recrute.talent-soft.com",
    }),
    ("CREDIT MUTUEL ARKEA", {
        "tags": ["Cyber", "IA", "Data", "Systèmes", "Développement"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Crédit Mutuel Arkéa (siège près de Brest/Relecq-Kerhuon) : gros employeur IT interne en cybersécurité bancaire, fraude IA, cloud et architecture SI.",
        "site": "https://www.arkea.com",
        "careers": "https://cmarkea-recrute.talent-soft.com",
    }),
    ("SOPRA STERIA", {
        "tags": ["Cyber", "Défense", "Développement", "Cloud", "Systèmes"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "ESN majeure présente notamment sur Brest / Guipavas avec de forts projets défense, sécurité, cloud et génie logiciel.",
        "site": "https://www.soprasteria.com",
        "careers": "https://jobs.smartrecruiters.com/SopraSteria1",
    }),
    ("CAPGEMINI", {
        "tags": ["Cyber", "IA", "Cloud", "Systèmes", "Développement"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "ESN internationale implantée à Brest et Caen : intégration systèmes, cloud, data/IA, cybersécurité et conseil en ingénierie.",
        "site": "https://www.capgemini.com/fr-fr/",
        "careers": "https://www.capgemini.com/fr-fr/carrieres/",
    }),
    ("EXAIL", {
        "tags": ["Systèmes", "Défense", "IA", "Réseaux"],
        "taille": "ETI",
        "priorite": 1,
        "desc": "Groupe de haute technologie (navigation, robotique autonome sous-marine et photonique, présent à Lannion et Brest).",
        "site": "https://www.exail.com",
        "careers": "https://www.exail.com/careers",
    }),
    ("EKINOPS", {
        "tags": ["Réseaux", "Télécoms", "Cyber", "Systèmes"],
        "taille": "ETI",
        "priorite": 1,
        "desc": "Équipementier télécom français (site R&D à Lannion) concevant des routeurs, solutions SD-WAN, transport optique et sécurité réseau.",
        "site": "https://www.ekinops.com",
        "careers": "https://www.ekinops.com/company/careers",
    }),
    ("NXP SEMICONDUCTORS", {
        "tags": ["Cyber", "Systèmes", "Télécoms"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Leader mondial des semi-conducteurs implanté à Caen (Campus EffiScience) : R&D RFID/NFC, sécurité matérielle, cryptographie et systèmes.",
        "site": "https://www.nxp.com",
        "careers": "https://nxp.wd3.myworkdayjobs.com/careers",
    }),
    ("MURATA", {
        "tags": ["Systèmes", "Industrie"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Site industriel et R&D de haute technologie près de Caen (composants et systèmes intégrés).",
        "site": "https://www.murata.com",
        "careers": "https://www.murata.com/en-eu/about/company/europe",
    }),
    ("SAFRAN", {
        "tags": ["Défense", "Cyber", "IA", "Systèmes"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Groupe aéronautique et défense : ingénierie informatique, systèmes critiques, cybersécurité industrielle et IA.",
        "site": "https://www.safran-group.com",
        "careers": "https://www.safran-group.com/fr/offres",
    }),
    ("ALTEN", {
        "tags": ["Ingénierie", "Cyber", "IA", "Systèmes"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Leader mondial du conseil en ingénierie et R&D externalisée (défense, télécoms, informatique, systèmes).",
        "site": "https://www.alten.fr",
        "careers": "https://www.alten.fr/carriere/",
    }),
    ("CGI", {
        "tags": ["IT", "Cyber", "Cloud", "IA", "Développement"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Grande ESN internationale intervenant sur les systèmes d'information, la data/IA, le cloud et la cybersécurité.",
        "site": "https://www.cgi.com/france",
        "careers": "https://www.cgi.com/france/fr-fr/carrieres",
    }),
    ("EVIDEN", {
        "tags": ["Cyber", "IA", "Cloud", "Systèmes"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Branche cyber, cloud, HPC et IA du groupe Atos (SOC, chiffrement, souveraineté numérique).",
        "site": "https://eviden.com",
        "careers": "https://eviden.com/careers/",
    }),
    ("ATOS", {
        "tags": ["Cyber", "Cloud", "Systèmes", "IT"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Acteur majeur des services numériques, infogérance, cloud et cybersécurité.",
        "site": "https://atos.net",
        "careers": "https://atos.net/fr/carrieres",
    }),
    ("SII", {
        "tags": ["Défense", "Cyber", "Systèmes", "Développement"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Entreprise de services du numérique et conseil en ingénierie spécialisée dans la défense, les télécoms et l'IT.",
        "site": "https://sii-group.com/fr-FR",
        "careers": "https://sii-group.com/fr-FR/rejoindre-sii",
    }),
    ("INFOTEL", {
        "tags": ["Développement", "Systèmes", "IA", "Cloud"],
        "taille": "ETI",
        "priorite": 1,
        "desc": "ESN et éditeur de logiciels français intervenant dans la banque/assurance, le cloud et la data (présente dans l'Ouest).",
        "site": "https://www.infotel.com",
        "careers": "https://carrieres.infotel.com",
    }),
    ("IFREMER", {
        "tags": ["Recherche", "IA", "Data", "Systèmes", "Réseaux"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Institut de recherche océanographique basé à Plouzané : calcul scientifique, infrastructures systèmes/réseaux, data et IA appliquée.",
        "site": "https://www.ifremer.fr",
        "careers": "https://www.ifremer.fr/fr/nous-rejoindre",
    }),
    ("IMT ATLANTIQUE", {
        "tags": ["Recherche", "Cyber", "IA", "Réseaux"],
        "taille": "ETI",
        "priorite": 1,
        "desc": "Grande école d'ingénieurs et laboratoires de recherche de pointe à Plouzané (cybersécurité, réseaux, IA, Lab-STICC).",
        "site": "https://www.imt-atlantique.fr",
        "careers": "https://www.imt-atlantique.fr/fr/ecole/travailler-imt-atlantique",
    }),
    ("ENSTA", {
        "tags": ["Recherche", "Cyber", "IA", "Défense", "Systèmes"],
        "taille": "ETI",
        "priorite": 1,
        "desc": "École d'ingénieurs et pôles R&D à Brest (sécurité des systèmes, cyber maritime, défense et robotique).",
        "site": "https://www.ensta-bretagne.fr",
        "careers": "https://www.ensta-bretagne.fr",
    }),
    ("CNRS", {
        "tags": ["Recherche", "IA", "Cyber", "Systèmes"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Unités de recherche publiques (ex. GREYC à Caen, Lab-STICC à Brest) travaillant en IA, sécurité informatique et systèmes.",
        "site": "https://www.cnrs.fr",
        "careers": "https://emploi.cnrs.fr",
    }),
    ("OPEN", {
        "tags": ["IT", "Développement", "Cloud", "Systèmes"],
        "taille": "ETI",
        "priorite": 1,
        "desc": "Entreprise de services du numérique accompagnant la transformation des SI et infrastructures cloud.",
        "site": "https://www.open.global",
        "careers": "https://www.open.global/nous-rejoindre/",
    }),
    ("ACCENTURE", {
        "tags": ["IT", "Cyber", "IA", "Cloud"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Cabinet mondial de conseil en technologies, cloud, IA et cybersécurité.",
        "site": "https://www.accenture.com/fr-fr",
        "careers": "https://www.accenture.com/fr-fr/careers",
    }),
    ("OVH", {
        "tags": ["Cloud", "Systèmes", "Réseaux", "Cyber"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Fournisseur cloud européen (infrastructure, serveurs, réseaux et sécurité).",
        "site": "https://www.ovhcloud.com/fr/",
        "careers": "https://corporate.ovhcloud.com/fr/careers/",
    }),
    ("INETUM", {
        "tags": ["IT", "Cloud", "Cyber", "Développement"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "ESN européenne présente dans l'Ouest (Brest / Normandie) : conseil IT, cybersécurité, cloud et applications.",
        "site": "https://www.inetum.com/fr",
        "careers": "https://www.inetum.com/fr/carrieres",
    }),
    ("DASSAULT", {
        "tags": ["Défense", "IA", "Systèmes", "Cyber"],
        "taille": "Grand groupe",
        "priorite": 1,
        "desc": "Groupe aéronautique, défense et logiciels industriels.",
        "site": "https://www.3ds.com/fr",
        "careers": "https://careers.3ds.com",
    }),
]

MAJOR_NAME_KEYWORDS = (
    "THALES", "NAVAL", "NOKIA", "ORANGE", "ARKEA", "CAPGEMINI", "SOPRA", "ALTEN",
    "CGI", "ATOS", "EVIDEN", "SII", "INFOTEL", "EXAIL", "EKINOPS", "NXP", "SAFRAN",
    "IFREMER", "IMT", "ENSTA", "ENSSAT", "ENSICAEN", "CNRS", "GREYC", "CYBER",
    "INFORMATIQUE", "LOGICIEL", "RESEAU", "SYSTEME", "SECURITE", "TELECOM", "DATA",
    "INTELLIGENCE ARTIFICIELLE", "CLOUD", "DIGITAL", "NUMERIQUE",
)

EMBARQUE_PENALTY_KEYWORDS = (
    "MECANIQUE", "BATIMENT", "CHAUFFAGE", "PLOMBERIE", "TOITURE", "MACONNERIE",
    "BOULANGER", "COIFFURE", "CARRELAGE", "CHARPENTE",
)


def _norm(txt: str | None) -> str:
    s = unicodedata.normalize("NFKD", txt or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.upper().split())


def _match_flagship(name: str) -> dict[str, Any] | None:
    n = _norm(name)
    for kw, prof in FLAGSHIP_PROFILES:
        if kw in n:
            return prof
    return None


def _size_bucket(cat_ent: str | None, tranche: str | None, flagship: dict[str, Any] | None) -> tuple[str, int]:
    if flagship:
        t = flagship["taille"]
        return t, flagship["priorite"]
    if cat_ent == "GE" or tranche in {"51", "52", "53"}:
        return "Grand groupe", 1
    if cat_ent == "ETI" or tranche in {"32", "41", "42"}:
        return "ETI", 1
    if tranche in {"21", "22", "31"}:
        return "PME", 2
    return "Petite entreprise", 3


def _build_description(
    nom: str,
    naf: str,
    naf_desc: str,
    effectif_label: str,
    ville: str,
    cat_ent: str | None,
    flagship: dict[str, Any] | None,
) -> str:
    if flagship:
        return f"{flagship['desc']} ({effectif_label} sur site · {ville} · NAF {naf})."
    size_txt = f"{cat_ent} · " if cat_ent in {"GE", "ETI"} else ""
    return f"{naf_desc}. Structure {size_txt}{effectif_label} implantée à {ville} (NAF {naf})."


def _fetch_sirene_page(
    client: httpx.Client,
    lat: float,
    lon: float,
    naf_codes: str,
    tranches: str,
    page: int,
) -> dict[str, Any]:
    params = {
        "lat": lat,
        "long": lon,
        "radius": 32,
        "etat_administratif": "A",
        "activite_principale": naf_codes,
        "tranche_effectif_salarie": tranches,
        "per_page": 25,
        "page": page,
        "limite_matching_etablissements": 10,
    }
    for attempt in range(3):
        resp = client.get(SIRENE_NEAR_URL, params=params)
        if resp.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return {}


def _upsert_company(cur, row: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO entreprises (
            siret, siren, nom, nom_commercial, zone, ville, code_postal, adresse,
            lat, lon, trajet_tranche, distance_km, code_naf, libelle_naf,
            tranche_effectif, effectif_label, taille_categorie, domaine_tags,
            priorite, description, site_web, url_carrieres, url_lba, lba_potentiel,
            source, updated_at
        )
        VALUES (
            %(siret)s, %(siren)s, %(nom)s, %(nom_commercial)s, %(zone)s, %(ville)s, %(code_postal)s, %(adresse)s,
            %(lat)s, %(lon)s, %(trajet_tranche)s, %(distance_km)s, %(code_naf)s, %(libelle_naf)s,
            %(tranche_effectif)s, %(effectif_label)s, %(taille_categorie)s, %(domaine_tags)s,
            %(priorite)s, %(description)s, %(site_web)s, %(url_carrieres)s, %(url_lba)s, %(lba_potentiel)s,
            %(source)s, NOW()
        )
        ON CONFLICT (siret) DO UPDATE SET
            siren = COALESCE(EXCLUDED.siren, entreprises.siren),
            nom = EXCLUDED.nom,
            nom_commercial = COALESCE(EXCLUDED.nom_commercial, entreprises.nom_commercial),
            zone = EXCLUDED.zone,
            ville = COALESCE(EXCLUDED.ville, entreprises.ville),
            code_postal = COALESCE(EXCLUDED.code_postal, entreprises.code_postal),
            adresse = COALESCE(EXCLUDED.adresse, entreprises.adresse),
            lat = COALESCE(EXCLUDED.lat, entreprises.lat),
            lon = COALESCE(EXCLUDED.lon, entreprises.lon),
            trajet_tranche = COALESCE(EXCLUDED.trajet_tranche, entreprises.trajet_tranche),
            distance_km = COALESCE(EXCLUDED.distance_km, entreprises.distance_km),
            code_naf = COALESCE(EXCLUDED.code_naf, entreprises.code_naf),
            libelle_naf = COALESCE(EXCLUDED.libelle_naf, entreprises.libelle_naf),
            tranche_effectif = COALESCE(EXCLUDED.tranche_effectif, entreprises.tranche_effectif),
            effectif_label = COALESCE(EXCLUDED.effectif_label, entreprises.effectif_label),
            taille_categorie = COALESCE(EXCLUDED.taille_categorie, entreprises.taille_categorie),
            domaine_tags = COALESCE(EXCLUDED.domaine_tags, entreprises.domaine_tags),
            priorite = LEAST(entreprises.priorite, EXCLUDED.priorite),
            description = COALESCE(NULLIF(entreprises.description, ''), EXCLUDED.description),
            site_web = COALESCE(NULLIF(entreprises.site_web, ''), EXCLUDED.site_web),
            url_carrieres = COALESCE(NULLIF(entreprises.url_carrieres, ''), EXCLUDED.url_carrieres),
            url_lba = COALESCE(NULLIF(EXCLUDED.url_lba, ''), entreprises.url_lba),
            lba_potentiel = entreprises.lba_potentiel OR EXCLUDED.lba_potentiel,
            updated_at = NOW()
        """,
        row,
    )


def _sync_sirene_zone(
    cur,
    client: httpx.Client,
    zone_code: str,
    zone_data: dict[str, Any],
    naf_dict: dict[str, tuple[str, list[str]]],
    require_major: bool,
) -> int:
    count = 0
    lat0, lon0 = float(zone_data["lat"]), float(zone_data["lon"])
    iso20 = zone_data["iso20_geojson"]
    iso30 = zone_data["iso30_geojson"]
    naf_codes = ",".join(naf_dict.keys())
    tranches = "32,41,42,51,52,53" if require_major else TRANCHES_GE_10

    page = 1
    max_pages = 30
    while page <= max_pages:
        payload = _fetch_sirene_page(client, lat0, lon0, naf_codes, tranches, page)
        results = payload.get("results") or []
        if not results:
            break
        total_pages = min(int(payload.get("total_pages") or 1), max_pages)

        for comp in results:
            cat_ent = comp.get("categorie_entreprise")
            comp_name = (comp.get("nom_complet") or comp.get("nom_raison_sociale") or "").strip()
            comp_norm = _norm(comp_name)
            if not comp_name or any(bad in comp_norm for bad in EMBARQUE_PENALTY_KEYWORDS):
                continue

            flagship = _match_flagship(comp_name)
            if require_major:
                if not flagship and cat_ent not in {"GE", "ETI"} and not any(k in comp_norm for k in MAJOR_NAME_KEYWORDS):
                    continue

            for est in comp.get("matching_etablissements") or []:
                if est.get("etat_administratif") != "A":
                    continue
                siret = (est.get("siret") or "").strip()
                if not siret:
                    continue
                try:
                    elat = float(est.get("latitude"))
                    elon = float(est.get("longitude"))
                except (TypeError, ValueError):
                    continue

                trajet = classify_travel_time(elon, elat, iso20, iso30)
                if not trajet:
                    continue

                local_tr = est.get("tranche_effectif_salarie") or "NN"
                if local_tr not in VALID_LOCAL_TRANCHES:
                    # Autoriser un grand acteur identifié (flagship) s'il n'est pas connu comme 0-2 salariés
                    if not (flagship and local_tr == "NN"):
                        continue

                naf = est.get("activite_principale") or comp.get("activite_principale") or ""
                if naf not in naf_dict:
                    naf = comp.get("activite_principale") or naf
                naf_desc, tags = naf_dict.get(naf, ("Activité technologique ou d'ingénierie", ["Tech"]))
                if flagship:
                    tags = flagship["tags"]

                eff_tr = local_tr if local_tr in VALID_LOCAL_TRANCHES else (comp.get("tranche_effectif_salarie") or "NN")
                eff_label = EFFECTIF_LABELS.get(eff_tr, "Non renseigné")
                taille_cat, prio = _size_bucket(cat_ent, eff_tr, flagship)
                ville = (est.get("libelle_commune") or "").title()
                cp = est.get("code_postal") or ""
                adresse = est.get("adresse") or ""
                dist = haversine_km(lat0, lon0, elat, elon)

                row = {
                    "siret": siret,
                    "siren": comp.get("siren") or siret[:9],
                    "nom": comp_name,
                    "nom_commercial": est.get("nom_commercial"),
                    "zone": zone_code,
                    "ville": ville,
                    "code_postal": cp,
                    "adresse": adresse,
                    "lat": elat,
                    "lon": elon,
                    "trajet_tranche": trajet,
                    "distance_km": dist,
                    "code_naf": naf,
                    "libelle_naf": naf_desc,
                    "tranche_effectif": eff_tr,
                    "effectif_label": eff_label,
                    "taille_categorie": taille_cat,
                    "domaine_tags": tags,
                    "priorite": prio,
                    "description": _build_description(comp_name, naf, naf_desc, eff_label, ville, cat_ent, flagship),
                    "site_web": flagship["site"] if flagship else None,
                    "url_carrieres": flagship["careers"] if flagship else None,
                    "url_lba": None,
                    "lba_potentiel": False,
                    "source": "sirene",
                }
                _upsert_company(cur, row)
                count += 1

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.18)

    return count


def _sync_lba_recruiters_zone(
    cur,
    client: httpx.Client,
    lba_token: str,
    zone_code: str,
    zone_data: dict[str, Any],
) -> dict[str, int]:
    lat0, lon0 = float(zone_data["lat"]), float(zone_data["lon"])
    iso20 = zone_data["iso20_geojson"]
    iso30 = zone_data["iso30_geojson"]

    params = {
        "latitude": lat0,
        "longitude": lon0,
        "radius": 35,
        "romes": "M1805,M1802,M1801,M1803,M1804,M1806,M1810,I1401",
        "caller": "homelab_career_radar",
    }
    resp = client.get(
        LBA_SEARCH_URL,
        params=params,
        headers={"Authorization": f"Bearer {lba_token}", "Accept": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    recruiters = data.get("recruiters") or []

    matched_existing = 0
    inserted_new = 0
    for rec in recruiters:
        wp = rec.get("workplace") or {}
        apply = rec.get("apply") or {}
        siret = (wp.get("siret") or "").strip()
        if not siret or len(siret) != 14:
            continue

        loc = wp.get("location") or {}
        coords = (loc.get("geopoint") or {}).get("coordinates") or []
        if len(coords) != 2:
            continue
        elon, elat = float(coords[0]), float(coords[1])
        trajet = classify_travel_time(elon, elat, iso20, iso30)
        if not trajet:
            continue

        url_lba = apply.get("url")
        website = wp.get("website")
        wp_desc = (wp.get("description") or "").strip() or None
        naf_obj = ((wp.get("domain") or {}).get("naf")) or {}
        naf_code = naf_obj.get("code") or ""
        naf_label = naf_obj.get("label") or "Recruteur potentiel alternance identifié par La Bonne Alternance"
        name = (wp.get("brand") or wp.get("legal_name") or wp.get("name") or "").strip()
        if not name:
            continue

        cur.execute("SELECT id, description, site_web FROM entreprises WHERE siret = %s", (siret,))
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                UPDATE entreprises SET
                    lba_potentiel = TRUE,
                    url_lba = COALESCE(%s, url_lba),
                    site_web = COALESCE(NULLIF(site_web, ''), %s),
                    description = CASE WHEN %s IS NOT NULL AND (description IS NULL OR LENGTH(description) < 90) THEN %s ELSE description END,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (url_lba, website, wp_desc, wp_desc, existing["id"]),
            )
            matched_existing += 1
            continue

        # Only insert standalone LBA recruiters if tech NAF & non-micro size (if size provided)
        size_str = str(wp.get("size") or "")
        if size_str in {"0-0", "1-2", "3-5", "6-9"}:
            continue
        if naf_code not in CORE_TECH_NAF and naf_code not in MAJOR_TECH_NAF and not _match_flagship(name):
            continue

        addr = loc.get("address") or ""
        parts = addr.split()
        cp = next((p for p in parts if len(p) == 5 and p.isdigit()), "")
        ville = addr.split(cp, 1)[-1].strip().title() if cp and cp in addr else ""
        flagship = _match_flagship(name)
        taille_cat, prio = _size_bucket(None, "21", flagship)
        desc = wp_desc or f"{naf_label}. Recruteur identifié à fort potentiel alternance (La Bonne Alternance · {addr})."

        _upsert_company(
            cur,
            {
                "siret": siret,
                "siren": siret[:9],
                "nom": name,
                "nom_commercial": wp.get("brand"),
                "zone": zone_code,
                "ville": ville or addr,
                "code_postal": cp,
                "adresse": addr,
                "lat": elat,
                "lon": elon,
                "trajet_tranche": trajet,
                "distance_km": haversine_km(lat0, lon0, elat, elon),
                "code_naf": naf_code,
                "libelle_naf": naf_label,
                "tranche_effectif": None,
                "effectif_label": size_str or "10+ salariés",
                "taille_categorie": taille_cat,
                "domaine_tags": flagship["tags"] if flagship else CORE_TECH_NAF.get(naf_code, ("", ["IT", "Alternance"]))[1],
                "priorite": prio,
                "description": desc,
                "site_web": website or (flagship["site"] if flagship else None),
                "url_carrieres": flagship["careers"] if flagship else None,
                "url_lba": url_lba,
                "lba_potentiel": True,
                "source": "lba_recruiter",
            },
        )
        inserted_new += 1

    return {"lba_matched": matched_existing, "lba_inserted": inserted_new, "lba_total_raw": len(recruiters)}


def sync_all_companies(cur, force_refresh_zones: bool = False) -> dict[str, Any]:
    """Synchronize IGN isochrones + SIRENE establishments + LBA potential recruiters across all 3 basins."""
    zones = ensure_zones_isochrones(cur, force_refresh=force_refresh_zones)
    lba_token = os.environ.get("LBA_API_TOKEN", "").strip()
    summary: dict[str, Any] = {"zones": {}}

    with httpx.Client(timeout=25.0, headers={"User-Agent": "Homelab-Career-Radar/1.0"}) as client:
        for zcode in ZONES:
            zdata = zones[zcode]
            core_cnt = _sync_sirene_zone(cur, client, zcode, zdata, CORE_TECH_NAF, require_major=False)
            major_cnt = _sync_sirene_zone(cur, client, zcode, zdata, MAJOR_TECH_NAF, require_major=True)
            lba_info: dict[str, int] = {}
            if lba_token:
                try:
                    lba_info = _sync_lba_recruiters_zone(cur, client, lba_token, zcode, zdata)
                except Exception as exc:
                    logger.warning("LBA recruiters sync failed for %s: %s", zcode, exc)
                    lba_info = {"error": str(exc)[:200]}
            summary["zones"][zcode] = {
                "sirene_core_upserts": core_cnt,
                "sirene_major_upserts": major_cnt,
                **lba_info,
            }

    cur.execute(
        """
        SELECT zone, trajet_tranche, COUNT(*)::int AS total,
               SUM(CASE WHEN lba_potentiel THEN 1 ELSE 0 END)::int AS lba_potentiel,
               SUM(CASE WHEN priorite = 1 THEN 1 ELSE 0 END)::int AS prio1
        FROM entreprises
        GROUP BY zone, trajet_tranche
        ORDER BY zone, trajet_tranche
        """
    )
    summary["db_totals"] = [dict(r) for r in cur.fetchall()]
    return summary
