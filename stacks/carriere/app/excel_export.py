"""Formatted Excel (.xlsx) workbook generator for Career Radar."""
from __future__ import annotations

import io
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="F8FAFC")
LINK_FONT = Font(name="Calibri", size=10, color="2563EB", underline="single")
BODY_FONT = Font(name="Calibri", size=10, color="0F172A")

STATUS_COLORS: dict[str, tuple[str, str]] = {
    "Accepté": ("DCFCE7", "166534"),
    "Entretien": ("D1FAE5", "065F46"),
    "À postuler": ("FEF3C7", "92400E"),
    "À contacter": ("FEF3C7", "92400E"),
    "Postulée": ("DBEAFE", "1E40AF"),
    "Postulé": ("DBEAFE", "1E40AF"),
    "Contactée": ("DBEAFE", "1E40AF"),
    "Contacté": ("DBEAFE", "1E40AF"),
    "Relancée": ("E0E7FF", "3730A3"),
    "Relancé": ("E0E7FF", "3730A3"),
    "En attente": ("EDE9FE", "5B21B6"),
    "Refusé": ("FEE2E2", "991B1B"),
    "Refusée": ("FEE2E2", "991B1B"),
    "Refus": ("FEE2E2", "991B1B"),
    "Ignoré": ("F1F5F9", "64748B"),
    "Ignorée": ("F1F5F9", "64748B"),
    "Écartée": ("F1F5F9", "64748B"),
}


def _style_sheet(ws, headers: list[str], rows: list[list], link_cols: set[int], status_col: int | None, widths: list[int]):
    ws.append(headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 24

    for row_idx, rdata in enumerate(rows, start=2):
        ws.append(rdata)
        ws.row_dimensions[row_idx].height = 20
        for col_idx, val in enumerate(rdata, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = BODY_FONT
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            if col_idx in link_cols and isinstance(val, str) and val.startswith(("http://", "https://")):
                cell.hyperlink = val
                cell.font = LINK_FONT
            if status_col == col_idx and isinstance(val, str) and val in STATUS_COLORS:
                bg, fg = STATUS_COLORS[val]
                cell.fill = PatternFill(start_color=bg, end_color=bg, fill_type="solid")
                cell.font = Font(name="Calibri", size=10, bold=True, color=fg)

    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"


def _format_stage_duration(o: dict) -> str:
    if o.get("type_contrat") != "stage":
        return ""
    dmin = o.get("duree_semaines_min")
    dmax = o.get("duree_semaines_max")
    if dmin is None:
        return "Non précisée"
    if dmax is None or dmin == dmax:
        return f"{dmin} sem."
    return f"{dmin}-{dmax} sem."


def _build_offer_rows(items: Iterable[dict]) -> list[list]:
    return [
        [
            o["id"],
            "★" if o.get("is_starred") else "",
            o.get("statut") or "À voir",
            o.get("type_contrat") or "",
            _format_stage_duration(o),
            str(o["date_debut_stage"]) if o.get("date_debut_stage") else "",
            o.get("score_ia"),
            o.get("domaine_ia") or "",
            o.get("zone") or "",
            o.get("trajet_tranche") or "",
            o.get("ville") or "",
            o.get("entreprise_nom") or "",
            o.get("entreprise_resume") or "",
            o.get("titre") or "",
            o.get("resume_ia") or "",
            str(o["date_publication"]) if o.get("date_publication") else "",
            str(o["date_candidature"]) if o.get("date_candidature") else "",
            str(o["date_relance"]) if o.get("date_relance") else "",
            o.get("contact_info") or "",
            o.get("source") or "",
            o.get("url_directe") or "",
            o.get("notes") or "",
        ]
        for o in items
    ]


def build_workbook_bytes(offres: Iterable[dict], entreprises: Iterable[dict]) -> bytes:
    """Create a 3-sheet .xlsx workbook containing Offres, Sélection, and Entreprises."""
    wb = Workbook()
    offres_list = list(offres)

    o_headers = [
        "ID", "Pointée", "Statut", "Contrat", "Durée stage", "Début stage",
        "Score IA", "Domaine", "Zone", "Trajet voiture",
        "Ville", "Entreprise", "Activité entreprise", "Titre du poste", "Résumé IA",
        "Date publi.", "Date candidature", "Date relance", "Contact", "Source",
        "Lien direct", "Notes",
    ]
    o_widths = [7, 9, 15, 13, 14, 13, 10, 18, 12, 14, 18, 28, 42, 42, 48, 12, 15, 14, 24, 18, 45, 36]

    ws_offres = wb.active
    ws_offres.title = "Offres"
    _style_sheet(ws_offres, o_headers, _build_offer_rows(offres_list), link_cols={21}, status_col=3, widths=o_widths)

    ws_sel = wb.create_sheet(title="Sélection")
    starred_list = [o for o in offres_list if o.get("is_starred")]
    _style_sheet(ws_sel, o_headers, _build_offer_rows(starred_list), link_cols={21}, status_col=3, widths=o_widths)

    ws_ent = wb.create_sheet(title="Entreprises")
    e_headers = [
        "ID", "Priorité", "Statut", "Zone", "Trajet voiture", "Dist. km",
        "Nom", "Ville", "CP", "Taille", "Effectif", "Code NAF", "Secteur NAF",
        "Domaines", "Activité / À savoir avant de contacter", "Potentiel LBA",
        "Site web", "Page carrières", "Fiche LBA", "Contact nom", "Contact email",
        "Date contact", "Date relance", "Notes", "SIRET",
    ]
    e_rows = [
        [
            e["id"],
            e.get("priorite"),
            e.get("statut") or "Non contactée",
            e.get("zone") or "",
            e.get("trajet_tranche") or "",
            e.get("distance_km"),
            e.get("nom") or "",
            e.get("ville") or "",
            e.get("code_postal") or "",
            e.get("taille_categorie") or "",
            e.get("effectif_label") or "",
            e.get("code_naf") or "",
            e.get("libelle_naf") or "",
            ", ".join(e.get("domaine_tags") or []),
            e.get("description") or "",
            "Oui" if e.get("lba_potentiel") else "Non",
            e.get("site_web") or "",
            e.get("url_carrieres") or "",
            e.get("url_lba") or "",
            e.get("contact_nom") or "",
            e.get("contact_email") or "",
            str(e["date_contact"]) if e.get("date_contact") else "",
            str(e["date_relance"]) if e.get("date_relance") else "",
            e.get("notes") or "",
            e.get("siret") or "",
        ]
        for e in entreprises
    ]
    _style_sheet(
        ws_ent,
        e_headers,
        e_rows,
        link_cols={17, 18, 19},
        status_col=3,
        widths=[7, 9, 16, 12, 14, 9, 32, 20, 8, 17, 19, 10, 34, 24, 52, 13, 32, 38, 38, 22, 26, 13, 13, 36, 17],
    )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
