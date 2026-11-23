"""FastAPI backend for Homelab Career Radar (web CRM, Excel export, internal n8n hooks)."""
from __future__ import annotations

from datetime import date
import hmac
import json
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

from .companies_sync import sync_all_companies
from .db import db_cursor
from .excel_export import build_workbook_bytes
from .offers_collect import collect_all_offers

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Homelab Career Radar", version="1.0.0")


def require_internal_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = os.environ.get("CARRIERE_INTERNAL_API_KEY", "").strip()
    if not expected or not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class OfferCreate(BaseModel):
    entreprise_nom: str = Field(..., min_length=1, max_length=200)
    titre: str = Field(..., min_length=1, max_length=300)
    type_contrat: str = Field(default="alternance")
    zone: str = Field(default="loperhet")
    ville: str | None = None
    url_directe: str = Field(..., min_length=4, max_length=1000)
    entreprise_resume: str | None = None
    description: str | None = None
    statut: str = Field(default="À postuler")
    date_candidature: date | None = None
    date_relance: date | None = None
    contact_info: str | None = None
    notes: str | None = None
    is_starred: bool = False


class OfferPatch(BaseModel):
    statut: str | None = None
    date_candidature: date | None = None
    date_relance: date | None = None
    contact_info: str | None = None
    notes: str | None = None
    resume_ia: str | None = None
    entreprise_resume: str | None = None
    is_starred: bool | None = None


class CompanyCreate(BaseModel):
    nom: str = Field(..., min_length=1, max_length=250)
    zone: str = Field(default="loperhet")
    ville: str | None = None
    trajet_tranche: str | None = "<=20min"
    taille_categorie: str | None = "PME"
    description: str | None = None
    site_web: str | None = None
    url_carrieres: str | None = None
    statut: str = Field(default="Non contactée")
    priorite: int = Field(default=2, ge=1, le=3)
    contact_nom: str | None = None
    contact_email: str | None = None
    date_contact: date | None = None
    date_relance: date | None = None
    notes: str | None = None


class CompanyPatch(BaseModel):
    statut: str | None = None
    contact_nom: str | None = None
    contact_email: str | None = None
    date_contact: date | None = None
    date_relance: date | None = None
    notes: str | None = None
    site_web: str | None = None
    url_carrieres: str | None = None
    description: str | None = None
    priorite: int | None = Field(default=None, ge=1, le=3)


class ScoreItem(BaseModel):
    id: int
    score_ia: int = Field(..., ge=0, le=100)
    domaine_ia: str | None = None
    resume_ia: str | None = None
    entreprise_resume: str | None = None


class ApplyScoresPayload(BaseModel):
    scores: list[ScoreItem] = Field(default_factory=list)


class MarkNotifiedPayload(BaseModel):
    ids: list[int] = Field(default_factory=list)


def _fetch_pending_notifications(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT o.id, o.source, o.titre, o.type_contrat, o.zone, o.ville,
               COALESCE(o.trajet_tranche, e.trajet_tranche, 'Zone cible') AS trajet_tranche,
               o.entreprise_nom,
               COALESCE(NULLIF(o.entreprise_resume, ''), e.description, '') AS entreprise_resume,
               o.url_directe, o.date_publication, o.score_ia, o.domaine_ia, o.resume_ia,
               e.taille_categorie, e.effectif_label
        FROM offres o
        LEFT JOIN entreprises e ON e.id = o.entreprise_id
        WHERE o.notified_at IS NULL
          AND COALESCE(o.score_ia, 0) >= 55
          AND o.statut NOT IN ('Ignorée', 'Ignoré', 'Refusée', 'Refusé')
        ORDER BY o.score_ia DESC, o.date_publication DESC NULLS LAST, o.id ASC
        LIMIT 25
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get("date_publication"):
            r["date_publication"] = str(r["date_publication"])
    return rows


def _fetch_unscored_offers(cur, limit: int = 25) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT o.id, o.source, o.titre, o.type_contrat, o.zone, o.ville,
               COALESCE(o.trajet_tranche, e.trajet_tranche, '') AS trajet_tranche,
               o.entreprise_nom,
               COALESCE(NULLIF(o.entreprise_resume, ''), e.description, '') AS entreprise_resume,
               LEFT(COALESCE(o.description, ''), 1400) AS description,
               e.taille_categorie, e.libelle_naf
        FROM offres o
        LEFT JOIN entreprises e ON e.id = o.entreprise_id
        WHERE o.score_ia IS NULL
          AND o.statut NOT IN ('Ignorée', 'Ignoré')
        ORDER BY o.created_at DESC, o.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def _serialize_dates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        for k, v in list(r.items()):
            if hasattr(v, "isoformat"):
                r[k] = v.isoformat()
    return rows


@app.get("/", response_class=HTMLResponse)
@app.get("/offres", response_class=HTMLResponse)
@app.get("/offres/stages", response_class=HTMLResponse)
@app.get("/offres/alternances", response_class=HTMLResponse)
@app.get("/selection", response_class=HTMLResponse)
@app.get("/selection/stages", response_class=HTMLResponse)
@app.get("/selection/alternances", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT COUNT(*)::int AS entreprises FROM entreprises")
        ec = cur.fetchone()["entreprises"]
        cur.execute("SELECT COUNT(*)::int AS offres FROM offres")
        oc = cur.fetchone()["offres"]
    return {"status": "ok", "db": "connected", "entreprises": ec, "offres": oc}


@app.get("/api/stats")
def get_stats():
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT statut, COUNT(*)::int AS total FROM offres GROUP BY statut ORDER BY total DESC")
        offres_statut = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT zone, COUNT(*)::int AS total FROM offres GROUP BY zone ORDER BY zone")
        offres_zone = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT type_contrat, COUNT(*)::int AS total FROM offres WHERE statut NOT IN ('Ignorée', 'Ignoré') GROUP BY type_contrat ORDER BY type_contrat"
        )
        offres_contrat = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT type_contrat, COUNT(*)::int AS total FROM offres WHERE is_starred = TRUE AND statut NOT IN ('Ignorée', 'Ignoré') GROUP BY type_contrat ORDER BY type_contrat"
        )
        selection_contrat = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT zone,
                   COUNT(*)::int AS total,
                   SUM(CASE WHEN trajet_tranche = '<=20min' THEN 1 ELSE 0 END)::int AS sous_20min,
                   SUM(CASE WHEN trajet_tranche = '20-30min' THEN 1 ELSE 0 END)::int AS entre_20_30min,
                   SUM(CASE WHEN priorite = 1 THEN 1 ELSE 0 END)::int AS grands_acteurs,
                   SUM(CASE WHEN lba_potentiel THEN 1 ELSE 0 END)::int AS lba_potentiel,
                   SUM(CASE WHEN statut NOT IN ('Non contactée', 'Écartée') THEN 1 ELSE 0 END)::int AS suivies
            FROM entreprises
            GROUP BY zone
            ORDER BY zone
            """
        )
        entreprises_zone = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT * FROM v_relances_a_faire LIMIT 50")
        relances = _serialize_dates([dict(r) for r in cur.fetchall()])

        cur.execute(
            "SELECT COUNT(*)::int AS cnt FROM offres WHERE is_starred = TRUE AND statut NOT IN ('Ignorée', 'Ignoré')"
        )
        offres_pointees = cur.fetchone()["cnt"]

        cur.execute("SELECT id, started_at, finished_at, type, status, summary FROM collectes ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        last_collect = _serialize_dates([dict(row)])[0] if row else None

    return {
        "offres_par_statut": offres_statut,
        "offres_par_zone": offres_zone,
        "offres_par_contrat": offres_contrat,
        "selection_par_contrat": selection_contrat,
        "offres_pointees": offres_pointees,
        "entreprises_par_zone": entreprises_zone,
        "relances": relances,
        "derniere_collecte": last_collect,
    }


@app.get("/api/offres")
def list_offres(
    zone: str | None = Query(default=None),
    type_contrat: str | None = Query(default=None),
    statut: str | None = Query(default=None),
    score_min: int | None = Query(default=None),
    starred: bool | None = Query(default=None),
    q: str | None = Query(default=None),
):
    where = ["1=1"]
    params: list[Any] = []
    if zone:
        where.append("o.zone = %s")
        params.append(zone)
    if type_contrat:
        where.append("o.type_contrat = %s")
        params.append(type_contrat)
    if statut:
        where.append("o.statut = %s")
        params.append(statut)
    else:
        where.append("o.statut NOT IN ('Ignorée', 'Ignoré')")
    if score_min is not None:
        where.append("COALESCE(o.score_ia, 0) >= %s")
        params.append(score_min)
    if starred is True:
        where.append("o.is_starred = TRUE")
    if q:
        where.append("(o.titre ILIKE %s OR o.entreprise_nom ILIKE %s OR COALESCE(o.ville,'') ILIKE %s OR COALESCE(o.resume_ia,'') ILIKE %s)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like, like])

    sql = f"""
        SELECT o.id, o.source, o.entreprise_id, o.entreprise_nom,
               COALESCE(NULLIF(o.entreprise_resume, ''), e.description, '') AS entreprise_resume,
               o.titre, o.type_contrat, o.zone, o.ville,
               COALESCE(o.trajet_tranche, e.trajet_tranche) AS trajet_tranche,
               o.url_directe, o.description, o.date_publication, o.score_ia, o.domaine_ia,
               o.resume_ia, o.statut, o.is_starred, o.date_candidature, o.date_relance,
               o.contact_info, o.notes, o.notified_at, o.created_at, o.updated_at,
               e.taille_categorie, e.effectif_label, e.site_web, e.url_carrieres
        FROM offres o
        LEFT JOIN entreprises e ON e.id = o.entreprise_id
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(o.score_ia, 50) DESC, o.date_publication DESC NULLS LAST, o.id DESC
        LIMIT 500
    """
    with db_cursor(commit=False) as cur:
        cur.execute(sql, params)
        return _serialize_dates([dict(r) for r in cur.fetchall()])


@app.post("/api/offres")
def create_offer(payload: OfferCreate):
    ext_id = f"manual-{os.urandom(6).hex()}"
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, description, trajet_tranche FROM entreprises WHERE LOWER(nom) LIKE %s ORDER BY priorite ASC LIMIT 1",
            (f"%{payload.entreprise_nom.strip().lower()}%",),
        )
        ent = cur.fetchone()
        cur.execute(
            """
            INSERT INTO offres (
                source, external_id, dedup_hash, entreprise_id, entreprise_nom, entreprise_resume,
                titre, type_contrat, zone, ville, trajet_tranche, url_directe, description,
                date_publication, score_ia, statut, is_starred, date_candidature, date_relance, contact_info, notes
            )
            VALUES (
                'manuel', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                CURRENT_DATE, 80, %s, %s, %s, %s, %s, %s
            )
            RETURNING *
            """,
            (
                ext_id,
                ext_id,
                ent["id"] if ent else None,
                payload.entreprise_nom.strip(),
                payload.entreprise_resume or (ent["description"] if ent else None),
                payload.titre.strip(),
                payload.type_contrat,
                payload.zone,
                payload.ville,
                ent["trajet_tranche"] if ent else None,
                payload.url_directe.strip(),
                payload.description,
                payload.statut,
                payload.is_starred,
                payload.date_candidature,
                payload.date_relance,
                payload.contact_info,
                payload.notes,
            ),
        )
        row = dict(cur.fetchone())
    return _serialize_dates([row])[0]


@app.patch("/api/offres/{offer_id}")
def patch_offer(offer_id: int, payload: OfferPatch):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    sets = [f"{k} = %s" for k in updates]
    sets.append("updated_at = NOW()")
    vals = list(updates.values()) + [offer_id]
    with db_cursor() as cur:
        cur.execute(f"UPDATE offres SET {', '.join(sets)} WHERE id = %s RETURNING *", vals)
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Offre introuvable")
        return _serialize_dates([dict(row)])[0]


@app.get("/api/entreprises")
def list_entreprises(
    zone: str | None = Query(default=None),
    trajet_tranche: str | None = Query(default=None),
    taille_categorie: str | None = Query(default=None),
    lba_potentiel: bool | None = Query(default=None),
    statut: str | None = Query(default=None),
    q: str | None = Query(default=None),
):
    where = ["1=1"]
    params: list[Any] = []
    if zone:
        where.append("e.zone = %s")
        params.append(zone)
    if trajet_tranche:
        where.append("e.trajet_tranche = %s")
        params.append(trajet_tranche)
    if taille_categorie:
        where.append("e.taille_categorie = %s")
        params.append(taille_categorie)
    if lba_potentiel is not None:
        where.append("e.lba_potentiel = %s")
        params.append(lba_potentiel)
    if statut:
        where.append("e.statut = %s")
        params.append(statut)
    if q:
        where.append("(e.nom ILIKE %s OR COALESCE(e.ville,'') ILIKE %s OR COALESCE(e.description,'') ILIKE %s OR COALESCE(e.libelle_naf,'') ILIKE %s)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like, like])

    sql = f"""
        SELECT e.*, COALESCE(oc.cnt, 0)::int AS nb_offres
        FROM entreprises e
        LEFT JOIN (
            SELECT entreprise_id, COUNT(*) AS cnt
            FROM offres
            WHERE entreprise_id IS NOT NULL
            GROUP BY entreprise_id
        ) oc ON oc.entreprise_id = e.id
        WHERE {' AND '.join(where)}
        ORDER BY e.priorite ASC, e.lba_potentiel DESC, e.distance_km ASC NULLS LAST, e.nom ASC
        LIMIT 1000
    """
    with db_cursor(commit=False) as cur:
        cur.execute(sql, params)
        return _serialize_dates([dict(r) for r in cur.fetchall()])


@app.post("/api/entreprises")
def create_company(payload: CompanyCreate):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO entreprises (
                siret, nom, zone, ville, trajet_tranche, taille_categorie,
                priorite, description, site_web, url_carrieres, statut,
                contact_nom, contact_email, date_contact, date_relance, notes, source
            )
            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'manuel')
            RETURNING *
            """,
            (
                payload.nom.strip(),
                payload.zone,
                payload.ville,
                payload.trajet_tranche,
                payload.taille_categorie,
                payload.priorite,
                payload.description,
                payload.site_web,
                payload.url_carrieres,
                payload.statut,
                payload.contact_nom,
                payload.contact_email,
                payload.date_contact,
                payload.date_relance,
                payload.notes,
            ),
        )
        return _serialize_dates([dict(cur.fetchone())])[0]


@app.patch("/api/entreprises/{company_id}")
def patch_company(company_id: int, payload: CompanyPatch):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    sets = [f"{k} = %s" for k in updates]
    sets.append("updated_at = NOW()")
    vals = list(updates.values()) + [company_id]
    with db_cursor() as cur:
        cur.execute(f"UPDATE entreprises SET {', '.join(sets)} WHERE id = %s RETURNING *", vals)
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Entreprise introuvable")
        return _serialize_dates([dict(row)])[0]


@app.get("/api/export.xlsx")
def export_excel():
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT o.*, COALESCE(NULLIF(o.entreprise_resume, ''), e.description, '') AS entreprise_resume
            FROM offres o
            LEFT JOIN entreprises e ON e.id = o.entreprise_id
            WHERE o.statut NOT IN ('Ignorée', 'Ignoré')
            ORDER BY COALESCE(o.score_ia, 50) DESC, o.id DESC
            """
        )
        offres = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT * FROM entreprises ORDER BY priorite ASC, lba_potentiel DESC, zone ASC, nom ASC"
        )
        entreprises = [dict(r) for r in cur.fetchall()]

    xlsx = build_workbook_bytes(offres, entreprises)
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="homelab_carriere_suivi.xlsx"'},
    )


def _run_collect_offers(trigger_type: str) -> dict[str, Any]:
    with db_cursor() as cur:
        cur.execute("INSERT INTO collectes (type, status) VALUES (%s, 'running') RETURNING id", (trigger_type,))
        cid = cur.fetchone()["id"]
    try:
        with db_cursor() as cur:
            summary = collect_all_offers(cur)
            unscored = _fetch_unscored_offers(cur, limit=25)
            pending = _fetch_pending_notifications(cur)
        with db_cursor() as cur:
            cur.execute(
                "UPDATE collectes SET finished_at = NOW(), status = 'success', summary = %s::jsonb WHERE id = %s",
                (json.dumps(summary), cid),
            )
        return {
            "collect_id": cid,
            "run_summary": summary,
            "unscored_offers": unscored,
            "pending_to_notify": pending,
        }
    except Exception as exc:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE collectes SET finished_at = NOW(), status = 'error', summary = %s::jsonb WHERE id = %s",
                (json.dumps({"error": str(exc)[:500]}), cid),
            )
        raise


@app.post("/api/ui/collect-offers")
def ui_collect_offers():
    return _run_collect_offers("ui_manual")


@app.post("/api/internal/sync-companies", dependencies=[Depends(require_internal_key)])
def internal_sync_companies(force: bool = Query(default=False)):
    with db_cursor() as cur:
        cur.execute("INSERT INTO collectes (type, status) VALUES ('companies', 'running') RETURNING id")
        cid = cur.fetchone()["id"]
    try:
        with db_cursor() as cur:
            summary = sync_all_companies(cur, force_refresh_zones=force)
        with db_cursor() as cur:
            cur.execute(
                "UPDATE collectes SET finished_at = NOW(), status = 'success', summary = %s::jsonb WHERE id = %s",
                (json.dumps(summary), cid),
            )
        return {"collect_id": cid, "summary": summary}
    except Exception as exc:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE collectes SET finished_at = NOW(), status = 'error', summary = %s::jsonb WHERE id = %s",
                (json.dumps({"error": str(exc)[:500]}), cid),
            )
        raise


@app.post("/api/internal/collect-offers", dependencies=[Depends(require_internal_key)])
def internal_collect_offers():
    return _run_collect_offers("n8n_offers")


@app.post("/api/internal/apply-scores", dependencies=[Depends(require_internal_key)])
def internal_apply_scores(payload: ApplyScoresPayload):
    updated = 0
    with db_cursor() as cur:
        for item in payload.scores:
            cur.execute(
                """
                UPDATE offres SET
                    score_ia = %s,
                    domaine_ia = COALESCE(%s, domaine_ia),
                    resume_ia = COALESCE(%s, resume_ia),
                    entreprise_resume = COALESCE(NULLIF(%s, ''), entreprise_resume),
                    statut = CASE WHEN %s < 55 AND statut = 'À voir' THEN 'Ignorée' ELSE statut END,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    item.score_ia,
                    item.domaine_ia,
                    item.resume_ia,
                    item.entreprise_resume,
                    item.score_ia,
                    item.id,
                ),
            )
            updated += cur.rowcount
        pending = _fetch_pending_notifications(cur)
    return {"updated": updated, "pending_to_notify": pending}


@app.post("/api/internal/mark-notified", dependencies=[Depends(require_internal_key)])
def internal_mark_notified(payload: MarkNotifiedPayload):
    if not payload.ids:
        return {"marked": 0}
    with db_cursor() as cur:
        cur.execute(
            "UPDATE offres SET notified_at = NOW(), updated_at = NOW() WHERE id = ANY(%s) AND notified_at IS NULL",
            (payload.ids,),
        )
        return {"marked": cur.rowcount}
