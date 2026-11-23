-- Homelab Career Radar — PostgreSQL Schema (`carriere` database)
-- Note: Executed by `postgres` / `ethan` owner, then grants applied to `carriere_app`.

CREATE TABLE IF NOT EXISTS zones (
    code VARCHAR(20) PRIMARY KEY,
    label TEXT NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    iso20_geojson JSONB,
    iso30_geojson JSONB,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS entreprises (
    id SERIAL PRIMARY KEY,
    siret VARCHAR(14) UNIQUE,
    siren VARCHAR(9),
    nom TEXT NOT NULL,
    nom_commercial TEXT,
    zone VARCHAR(20) NOT NULL,
    ville TEXT,
    code_postal VARCHAR(10),
    adresse TEXT,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    trajet_tranche VARCHAR(20),
    distance_km REAL,
    code_naf VARCHAR(10),
    libelle_naf TEXT,
    tranche_effectif VARCHAR(4),
    effectif_label TEXT,
    taille_categorie VARCHAR(30),
    domaine_tags TEXT[],
    priorite SMALLINT DEFAULT 2,
    description TEXT,
    site_web TEXT,
    url_carrieres TEXT,
    url_lba TEXT,
    lba_potentiel BOOLEAN DEFAULT FALSE,
    statut VARCHAR(30) NOT NULL DEFAULT 'Non contactée',
    contact_nom TEXT,
    contact_email TEXT,
    date_contact DATE,
    date_relance DATE,
    notes TEXT,
    source VARCHAR(40) DEFAULT 'sirene',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entreprises_zone_prio_statut ON entreprises (zone, priorite, statut);
CREATE INDEX IF NOT EXISTS idx_entreprises_siren ON entreprises (siren);
CREATE INDEX IF NOT EXISTS idx_entreprises_trajet ON entreprises (trajet_tranche);
CREATE INDEX IF NOT EXISTS idx_entreprises_lba ON entreprises (lba_potentiel);

CREATE TABLE IF NOT EXISTS offres (
    id SERIAL PRIMARY KEY,
    source VARCHAR(40) NOT NULL,
    external_id TEXT NOT NULL,
    dedup_hash VARCHAR(64) NOT NULL,
    entreprise_id INTEGER REFERENCES entreprises(id) ON DELETE SET NULL,
    entreprise_nom TEXT NOT NULL,
    entreprise_resume TEXT,
    titre TEXT NOT NULL,
    type_contrat VARCHAR(20) NOT NULL,
    zone VARCHAR(20) NOT NULL,
    ville TEXT,
    trajet_tranche VARCHAR(20),
    url_directe TEXT NOT NULL,
    description TEXT,
    date_publication DATE,
    score_ia SMALLINT,
    domaine_ia VARCHAR(60),
    resume_ia TEXT,
    statut VARCHAR(30) NOT NULL DEFAULT 'À voir',
    date_candidature DATE,
    date_relance DATE,
    contact_info TEXT,
    notes TEXT,
    is_starred BOOLEAN NOT NULL DEFAULT FALSE,
    notified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_offres_zone_statut_score ON offres (zone, statut, score_ia DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_offres_notified_at ON offres (notified_at);
CREATE INDEX IF NOT EXISTS idx_offres_dedup_hash ON offres (dedup_hash);
CREATE INDEX IF NOT EXISTS idx_offres_entreprise_id ON offres (entreprise_id);
CREATE INDEX IF NOT EXISTS idx_offres_starred ON offres (is_starred) WHERE is_starred = TRUE;

CREATE TABLE IF NOT EXISTS collectes (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    type VARCHAR(20),
    status VARCHAR(20),
    summary JSONB
);

CREATE OR REPLACE VIEW v_relances_a_faire AS
SELECT
    'offre' AS entite_type,
    id,
    entreprise_nom AS nom,
    titre AS detail,
    zone,
    statut,
    date_candidature AS date_action,
    date_relance,
    url_directe AS url
FROM offres
WHERE statut IN ('Postulée', 'Postulé', 'Contacté', 'En attente')
  AND date_candidature IS NOT NULL
  AND COALESCE(date_relance, date_candidature) <= CURRENT_DATE - INTERVAL '10 days'
UNION ALL
SELECT
    'entreprise' AS entite_type,
    id,
    nom,
    COALESCE(libelle_naf, '') AS detail,
    zone,
    statut,
    date_contact AS date_action,
    date_relance,
    COALESCE(url_carrieres, site_web, url_lba, '') AS url
FROM entreprises
WHERE statut IN ('Contactée', 'En attente')
  AND date_contact IS NOT NULL
  AND COALESCE(date_relance, date_contact) <= CURRENT_DATE - INTERVAL '10 days'
ORDER BY date_action ASC;
