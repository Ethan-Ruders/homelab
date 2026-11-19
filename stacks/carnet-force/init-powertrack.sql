-- =============================================================================
-- PowerTrack PostgreSQL Schema, Post-Processing Functions & Analytical Views
-- Target DB: powertrack (inside postgres-force, PostgreSQL 15)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Core Tables
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS seances (
    seance_id TEXT PRIMARY KEY,
    schema_version INT NOT NULL DEFAULT 2,
    export_date TIMESTAMPTZ NOT NULL,
    date_seance TIMESTAMPTZ NOT NULL,
    bloc_nom TEXT,
    bloc_date_debut TIMESTAMPTZ,
    bloc_squat_1rm NUMERIC(6,2),
    bloc_bench_1rm NUMERIC(6,2),
    bloc_deadlift_1rm NUMERIC(6,2),
    semaine_numero INT,
    semaine_nom TEXT NOT NULL,
    seance_titre TEXT,
    heure_debut TIMESTAMPTZ,
    duree_minutes INT,
    series_validees_count INT NOT NULL,
    series_programmees_count INT NOT NULL,
    taux_completion NUMERIC(5,3) NOT NULL,
    tonnage_total_kg NUMERIC(10,2) NOT NULL,
    raw_payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ressentis_seances (
    seance_id TEXT PRIMARY KEY REFERENCES seances(seance_id) ON DELETE CASCADE,
    poids_corps_kg NUMERIC(5,2),
    sommeil_heures NUMERIC(4,2),
    niveau_energie INT CHECK (niveau_energie BETWEEN 1 AND 5),
    fatigue_lombaire INT CHECK (fatigue_lombaire BETWEEN 1 AND 5),
    stress INT CHECK (stress BETWEEN 1 AND 5),
    hydratation_litres NUMERIC(4,2),
    douleur_signalee TEXT,
    notes_seance TEXT
);

CREATE TABLE IF NOT EXISTS series_effectuees (
    id BIGSERIAL PRIMARY KEY,
    seance_id TEXT NOT NULL REFERENCES seances(seance_id) ON DELETE CASCADE,
    exercice_ordre INT NOT NULL,
    exercice_nom TEXT NOT NULL,
    categorie TEXT NOT NULL,
    mouvement_parent TEXT NOT NULL,
    indications TEXT,
    serie_numero INT NOT NULL,
    reps_cibles INT NOT NULL,
    rpe_cible NUMERIC(3,1) NOT NULL,
    charge NUMERIC(6,2),
    rpe NUMERIC(3,1),
    delta_rpe NUMERIC(4,1),
    e1rm NUMERIC(6,2),
    est_validee BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seances_date ON seances(date_seance DESC);
CREATE INDEX IF NOT EXISTS idx_seances_bloc ON seances(bloc_nom, date_seance DESC);
CREATE INDEX IF NOT EXISTS idx_series_seance ON series_effectuees(seance_id);
CREATE INDEX IF NOT EXISTS idx_series_mouvement ON series_effectuees(mouvement_parent, est_validee);
CREATE INDEX IF NOT EXISTS idx_series_exercice ON series_effectuees(exercice_nom, est_validee);

-- -----------------------------------------------------------------------------
-- 2. Deterministic Math & Domain Helper Functions
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION powertrack_round_2_5(val NUMERIC)
RETURNS NUMERIC
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT ROUND(val / 2.5) * 2.5;
$$;

CREATE OR REPLACE FUNCTION powertrack_calc_e1rm(charge NUMERIC, reps INT, rpe NUMERIC)
RETURNS NUMERIC
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    reps_to_failure NUMERIC;
BEGIN
    IF charge IS NULL OR charge <= 0 OR reps IS NULL OR reps <= 0 OR rpe IS NULL OR rpe <= 0 OR rpe > 10 THEN
        RETURN NULL;
    END IF;
    reps_to_failure := reps::NUMERIC + (10.0 - rpe);
    IF reps_to_failure <= 1.0 THEN
        RETURN powertrack_round_2_5(charge);
    END IF;
    RETURN powertrack_round_2_5(charge * (1.0 + reps_to_failure / 30.0));
END;
$$;

CREATE OR REPLACE FUNCTION powertrack_calc_dots(total_kg NUMERIC, bw_kg NUMERIC)
RETURNS NUMERIC
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    bw DOUBLE PRECISION;
    denom DOUBLE PRECISION;
BEGIN
    IF total_kg IS NULL OR total_kg <= 0 OR bw_kg IS NULL OR bw_kg <= 0 THEN
        RETURN NULL;
    END IF;
    bw := bw_kg::DOUBLE PRECISION;
    denom := -307.75076
        + 24.0900756 * bw
        - 0.1918759221 * POWER(bw, 2)
        + 0.0007391293 * POWER(bw, 3)
        - 0.000001093 * POWER(bw, 4);
    IF denom <= 0 THEN
        RETURN NULL;
    END IF;
    RETURN ROUND(((total_kg::DOUBLE PRECISION * 500.0) / denom)::NUMERIC, 2);
END;
$$;

CREATE OR REPLACE FUNCTION powertrack_calc_ipf_gl(total_kg NUMERIC, bw_kg NUMERIC)
RETURNS NUMERIC
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    denom DOUBLE PRECISION;
BEGIN
    IF total_kg IS NULL OR total_kg <= 0 OR bw_kg IS NULL OR bw_kg <= 0 THEN
        RETURN NULL;
    END IF;
    denom := 1199.72839 - 1025.18162 * EXP(-0.00921 * bw_kg::DOUBLE PRECISION);
    IF denom <= 0 THEN
        RETURN NULL;
    END IF;
    RETURN ROUND(((total_kg::DOUBLE PRECISION * 100.0) / denom)::NUMERIC, 2);
END;
$$;

CREATE OR REPLACE FUNCTION powertrack_is_pain_active(douleur TEXT)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT NULLIF(BTRIM(douleur), '') IS NOT NULL
       AND LOWER(BTRIM(douleur)) NOT IN ('aucune', 'ras', 'none', '-');
$$;

CREATE OR REPLACE FUNCTION powertrack_infer_parent(ex_name TEXT, explicit_parent TEXT DEFAULT NULL)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    lower_name TEXT;
BEGIN
    IF explicit_parent IN ('squat', 'bench', 'deadlift', 'autre') THEN
        RETURN explicit_parent;
    END IF;
    lower_name := LOWER(COALESCE(ex_name, ''));
    IF lower_name LIKE '%bench%' OR lower_name LIKE '%développé couché%' OR lower_name LIKE '%developpe couche%'
       OR lower_name LIKE '%spoto%' OR lower_name LIKE '%larsen%' OR lower_name LIKE '%3ct%' THEN
        RETURN 'bench';
    END IF;
    IF lower_name LIKE '%squat%' THEN
        RETURN 'squat';
    END IF;
    IF lower_name LIKE '%deadlift%' OR lower_name LIKE '%soulevé de terre%' OR lower_name LIKE '%souleve de terre%'
       OR lower_name LIKE '%deficit%' OR lower_name LIKE '%déficit%' OR lower_name LIKE '%block pull%' THEN
        RETURN 'deadlift';
    END IF;
    RETURN 'autre';
END;
$$;

CREATE OR REPLACE FUNCTION powertrack_ffforce_status(total_kg NUMERIC, bw_kg NUMERIC DEFAULT NULL)
RETURNS JSONB
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    target_class INT;
    cat_name TEXT;
    thresholds JSONB := '{
      "74": [
        {"lvl":"R3","req":360.0},{"lvl":"R2","req":410.0},{"lvl":"R1","req":480.0},
        {"lvl":"N2","req":562.5},{"lvl":"N1","req":650.0},{"lvl":"Europe","req":695.0},{"lvl":"Monde","req":715.0}
      ],
      "83": [
        {"lvl":"R3","req":410.0},{"lvl":"R2","req":460.0},{"lvl":"R1","req":530.0},
        {"lvl":"N2","req":602.5},{"lvl":"N1","req":680.0},{"lvl":"Europe","req":740.0},{"lvl":"Monde","req":770.0}
      ]
    }'::JSONB;
    wc TEXT;
    arr JSONB;
    elem JSONB;
    reached TEXT;
    next_lvl JSONB;
    grilles JSONB := '{}'::JSONB;
BEGIN
    IF total_kg IS NULL OR total_kg <= 0 THEN
        RETURN NULL;
    END IF;
    target_class := CASE WHEN bw_kg IS NOT NULL AND bw_kg > 74.0 THEN 83 ELSE 74 END;
    cat_name := '-' || target_class::TEXT || ' kg';

    FOR wc IN SELECT unnest(ARRAY['74', '83']) LOOP
        arr := thresholds -> wc;
        reached := 'Sous R3';
        next_lvl := to_jsonb('Monde atteint'::TEXT);
        FOR elem IN SELECT value FROM jsonb_array_elements(arr) LOOP
            IF total_kg >= (elem->>'req')::NUMERIC THEN
                reached := elem->>'lvl';
            ELSIF next_lvl = to_jsonb('Monde atteint'::TEXT) THEN
                next_lvl := jsonb_build_object(
                    'niveau', elem->>'lvl',
                    'manque_kg', ROUND(((elem->>'req')::NUMERIC - total_kg)::NUMERIC, 1)
                );
            END IF;
        END LOOP;
        grilles := grilles || jsonb_build_object(
            '-' || wc || 'kg',
            jsonb_build_object(
                'niveau_atteint', reached,
                'prochain_palier', next_lvl
            )
        );
    END LOOP;

    RETURN jsonb_build_object(
        'categorie_suggeree', cat_name,
        'total_kg', ROUND(total_kg, 1),
        'grilles', grilles
    );
END;
$$;

-- -----------------------------------------------------------------------------
-- 3. Analytical Views (SBD, Intensity, Fatigue, Recovery, Alerts)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_seances_synthese AS
WITH series_agg AS (
    SELECT
        seance_id,
        MAX(CASE WHEN est_validee AND mouvement_parent = 'squat'
            THEN COALESCE(e1rm, powertrack_calc_e1rm(charge, reps_cibles, COALESCE(rpe, rpe_cible))) END) AS best_e1rm_squat,
        MAX(CASE WHEN est_validee AND mouvement_parent = 'bench'
            THEN COALESCE(e1rm, powertrack_calc_e1rm(charge, reps_cibles, COALESCE(rpe, rpe_cible))) END) AS best_e1rm_bench,
        MAX(CASE WHEN est_validee AND mouvement_parent = 'deadlift'
            THEN COALESCE(e1rm, powertrack_calc_e1rm(charge, reps_cibles, COALESCE(rpe, rpe_cible))) END) AS best_e1rm_deadlift,
        ROUND(AVG(CASE WHEN est_validee AND rpe IS NOT NULL THEN COALESCE(delta_rpe, rpe - rpe_cible) END)::NUMERIC, 2) AS delta_rpe_moyen,
        ROUND(MAX(CASE WHEN est_validee AND rpe IS NOT NULL THEN COALESCE(delta_rpe, rpe - rpe_cible) END)::NUMERIC, 1) AS delta_rpe_max,
        COUNT(*) FILTER (WHERE est_validee AND rpe IS NOT NULL AND COALESCE(delta_rpe, rpe - rpe_cible) >= 1.0)::INT AS overshoots_count,
        COUNT(*) FILTER (WHERE NOT est_validee)::INT AS series_non_validees_count,
        COUNT(*) FILTER (
            WHERE est_validee
              AND mouvement_parent IN ('squat', 'deadlift')
              AND categorie = 'competition'
              AND COALESCE(rpe, rpe_cible) >= 7.5
        )::INT AS series_axiales_sbd,
        COALESCE(SUM(CASE WHEN est_validee AND categorie = 'competition' AND charge > 0 THEN charge * reps_cibles ELSE 0 END), 0)::NUMERIC(10,1) AS tonnage_competition_kg,
        COALESCE(SUM(CASE WHEN est_validee AND categorie = 'polyarticulaire' AND charge > 0 THEN charge * reps_cibles ELSE 0 END), 0)::NUMERIC(10,1) AS tonnage_polyarticulaire_kg,
        COALESCE(SUM(CASE WHEN est_validee AND categorie = 'isolation' AND charge > 0 THEN charge * reps_cibles ELSE 0 END), 0)::NUMERIC(10,1) AS tonnage_isolation_kg,
        COALESCE(SUM(CASE WHEN est_validee AND mouvement_parent = 'squat' AND charge > 0 THEN charge * reps_cibles ELSE 0 END), 0)::NUMERIC(10,1) AS tonnage_squat_kg,
        COALESCE(SUM(CASE WHEN est_validee AND mouvement_parent = 'bench' AND charge > 0 THEN charge * reps_cibles ELSE 0 END), 0)::NUMERIC(10,1) AS tonnage_bench_kg,
        COALESCE(SUM(CASE WHEN est_validee AND mouvement_parent = 'deadlift' AND charge > 0 THEN charge * reps_cibles ELSE 0 END), 0)::NUMERIC(10,1) AS tonnage_deadlift_kg,
        COALESCE(SUM(CASE WHEN est_validee AND mouvement_parent = 'autre' AND charge > 0 THEN charge * reps_cibles ELSE 0 END), 0)::NUMERIC(10,1) AS tonnage_autre_kg
    FROM series_effectuees
    GROUP BY seance_id
)
SELECT
    s.seance_id,
    s.date_seance,
    s.bloc_nom,
    s.semaine_numero,
    s.semaine_nom,
    s.seance_titre,
    s.duree_minutes,
    s.series_validees_count,
    s.series_programmees_count,
    s.taux_completion,
    s.tonnage_total_kg,
    sa.best_e1rm_squat,
    sa.best_e1rm_bench,
    sa.best_e1rm_deadlift,
    sa.delta_rpe_moyen,
    sa.delta_rpe_max,
    COALESCE(sa.overshoots_count, 0) AS overshoots_count,
    COALESCE(sa.series_non_validees_count, 0) AS series_non_validees_count,
    COALESCE(sa.series_axiales_sbd, 0) AS series_axiales_sbd,
    COALESCE(sa.tonnage_competition_kg, 0) AS tonnage_competition_kg,
    COALESCE(sa.tonnage_polyarticulaire_kg, 0) AS tonnage_polyarticulaire_kg,
    COALESCE(sa.tonnage_isolation_kg, 0) AS tonnage_isolation_kg,
    COALESCE(sa.tonnage_squat_kg, 0) AS tonnage_squat_kg,
    COALESCE(sa.tonnage_bench_kg, 0) AS tonnage_bench_kg,
    COALESCE(sa.tonnage_deadlift_kg, 0) AS tonnage_deadlift_kg,
    COALESCE(sa.tonnage_autre_kg, 0) AS tonnage_autre_kg,
    r.poids_corps_kg,
    r.sommeil_heures,
    r.niveau_energie,
    r.fatigue_lombaire,
    r.stress,
    r.hydratation_litres,
    r.douleur_signalee,
    powertrack_is_pain_active(r.douleur_signalee) AS douleur_active,
    r.notes_seance,
    CASE
        WHEN s.duree_minutes > 120 THEN 'CRITIQUE'
        WHEN s.duree_minutes > 105 THEN 'ELEVEE'
        ELSE NULL
    END AS alerte_duree
FROM seances s
LEFT JOIN series_agg sa ON sa.seance_id = s.seance_id
LEFT JOIN ressentis_seances r ON r.seance_id = s.seance_id;

CREATE OR REPLACE VIEW v_records_mouvements AS
WITH ref AS (
    SELECT COALESCE(MAX(date_seance), NOW()) AS ref_date FROM seances
)
SELECT
    se.mouvement_parent,
    se.exercice_nom,
    MAX(CASE WHEN s.date_seance >= ref.ref_date - INTERVAL '7 days'
        THEN CASE WHEN se.mouvement_parent IN ('squat', 'bench', 'deadlift')
             THEN COALESCE(se.e1rm, powertrack_calc_e1rm(se.charge, se.reps_cibles, COALESCE(se.rpe, se.rpe_cible)))
             ELSE se.e1rm END END) AS best_e1rm_7j,
    MAX(CASE WHEN s.date_seance >= ref.ref_date - INTERVAL '14 days'
        THEN CASE WHEN se.mouvement_parent IN ('squat', 'bench', 'deadlift')
             THEN COALESCE(se.e1rm, powertrack_calc_e1rm(se.charge, se.reps_cibles, COALESCE(se.rpe, se.rpe_cible)))
             ELSE se.e1rm END END) AS best_e1rm_14j,
    MAX(CASE WHEN s.date_seance >= ref.ref_date - INTERVAL '28 days'
        THEN CASE WHEN se.mouvement_parent IN ('squat', 'bench', 'deadlift')
             THEN COALESCE(se.e1rm, powertrack_calc_e1rm(se.charge, se.reps_cibles, COALESCE(se.rpe, se.rpe_cible)))
             ELSE se.e1rm END END) AS best_e1rm_28j,
    MAX(CASE WHEN se.mouvement_parent IN ('squat', 'bench', 'deadlift')
        THEN COALESCE(se.e1rm, powertrack_calc_e1rm(se.charge, se.reps_cibles, COALESCE(se.rpe, se.rpe_cible)))
        ELSE se.e1rm END) AS best_e1rm_global,
    ROUND(AVG(CASE WHEN se.rpe IS NOT NULL THEN COALESCE(se.delta_rpe, se.rpe - se.rpe_cible) END)::NUMERIC, 2) AS delta_rpe_moyen,
    ROUND(MAX(CASE WHEN se.rpe IS NOT NULL THEN COALESCE(se.delta_rpe, se.rpe - se.rpe_cible) END)::NUMERIC, 1) AS delta_rpe_max,
    COUNT(*) FILTER (WHERE se.rpe IS NOT NULL AND COALESCE(se.delta_rpe, se.rpe - se.rpe_cible) >= 1.0)::INT AS overshoots_count,
    COUNT(*)::INT AS series_validees_count,
    MAX(s.date_seance) AS derniere_seance
FROM series_effectuees se
JOIN seances s ON s.seance_id = se.seance_id
CROSS JOIN ref
WHERE se.est_validee = TRUE
GROUP BY se.mouvement_parent, se.exercice_nom;

CREATE OR REPLACE VIEW v_records_par_bloc AS
SELECT
    COALESCE(s.bloc_nom, 'Sans bloc') AS bloc_nom,
    se.mouvement_parent,
    se.exercice_nom,
    MAX(CASE WHEN se.est_validee AND se.mouvement_parent IN ('squat', 'bench', 'deadlift')
        THEN COALESCE(se.e1rm, powertrack_calc_e1rm(se.charge, se.reps_cibles, COALESCE(se.rpe, se.rpe_cible)))
        WHEN se.est_validee THEN se.e1rm END) AS best_e1rm,
    ROUND(AVG(CASE WHEN se.est_validee AND se.rpe IS NOT NULL THEN COALESCE(se.delta_rpe, se.rpe - se.rpe_cible) END)::NUMERIC, 2) AS delta_rpe_moyen,
    ROUND(MAX(CASE WHEN se.est_validee AND se.rpe IS NOT NULL THEN COALESCE(se.delta_rpe, se.rpe - se.rpe_cible) END)::NUMERIC, 1) AS delta_rpe_max,
    COUNT(*) FILTER (WHERE se.est_validee AND se.rpe IS NOT NULL AND COALESCE(se.delta_rpe, se.rpe - se.rpe_cible) >= 1.0)::INT AS overshoots_count,
    COUNT(*) FILTER (WHERE NOT se.est_validee)::INT AS series_non_validees_count,
    COUNT(*) FILTER (WHERE se.est_validee)::INT AS series_validees_count
FROM series_effectuees se
JOIN seances s ON s.seance_id = se.seance_id
GROUP BY COALESCE(s.bloc_nom, 'Sans bloc'), se.mouvement_parent, se.exercice_nom;

CREATE OR REPLACE VIEW v_synthese_hebdo AS
WITH base AS (
    SELECT
        (DATE_TRUNC('week', date_seance AT TIME ZONE 'Europe/Paris'))::DATE AS semaine_debut,
        bloc_nom,
        semaine_numero,
        semaine_nom,
        COUNT(*)::INT AS nb_seances,
        ROUND(SUM(tonnage_total_kg)::NUMERIC, 1) AS tonnage_total_kg,
        ROUND(SUM(tonnage_squat_kg)::NUMERIC, 1) AS tonnage_squat_kg,
        ROUND(SUM(tonnage_bench_kg)::NUMERIC, 1) AS tonnage_bench_kg,
        ROUND(SUM(tonnage_deadlift_kg)::NUMERIC, 1) AS tonnage_deadlift_kg,
        ROUND(SUM(tonnage_autre_kg)::NUMERIC, 1) AS tonnage_autre_kg,
        ROUND(SUM(tonnage_competition_kg)::NUMERIC, 1) AS tonnage_competition_kg,
        ROUND(SUM(tonnage_polyarticulaire_kg)::NUMERIC, 1) AS tonnage_polyarticulaire_kg,
        ROUND(SUM(tonnage_isolation_kg)::NUMERIC, 1) AS tonnage_isolation_kg,
        ROUND(AVG(delta_rpe_moyen)::NUMERIC, 2) AS delta_rpe_moyen,
        ROUND(MAX(delta_rpe_max)::NUMERIC, 1) AS delta_rpe_max,
        SUM(overshoots_count)::INT AS overshoots_count,
        SUM(series_non_validees_count)::INT AS series_non_validees_count,
        SUM(series_axiales_sbd)::INT AS series_axiales_hebdo,
        ROUND(AVG(duree_minutes)::NUMERIC, 1) AS duree_moyenne_min,
        MAX(duree_minutes)::INT AS duree_max_min,
        ROUND(AVG(poids_corps_kg)::NUMERIC, 2) AS poids_corps_moyen_kg,
        ROUND(AVG(sommeil_heures)::NUMERIC, 2) AS sommeil_moyen_h,
        ROUND(AVG(hydratation_litres)::NUMERIC, 2) AS hydratation_moyenne_l,
        ROUND(AVG(fatigue_lombaire)::NUMERIC, 2) AS fatigue_lombaire_moyenne,
        ROUND(AVG(stress)::NUMERIC, 2) AS stress_moyen,
        MAX(best_e1rm_squat) AS best_e1rm_squat,
        MAX(best_e1rm_bench) AS best_e1rm_bench,
        MAX(best_e1rm_deadlift) AS best_e1rm_deadlift,
        ARRAY_REMOVE(ARRAY_AGG(douleur_signalee ORDER BY date_seance), NULL) AS douleurs_brutes,
        BOOL_OR(douleur_active) AS douleur_active_semaine
    FROM v_seances_synthese
    GROUP BY
        (DATE_TRUNC('week', date_seance AT TIME ZONE 'Europe/Paris'))::DATE,
        bloc_nom,
        semaine_numero,
        semaine_nom
)
SELECT
    b.*,
    CASE
        WHEN b.best_e1rm_squat > 0 AND b.best_e1rm_bench > 0 AND b.best_e1rm_deadlift > 0
        THEN ROUND((b.best_e1rm_squat + b.best_e1rm_bench + b.best_e1rm_deadlift)::NUMERIC, 1)
        ELSE NULL
    END AS total_estime_kg,
    CASE
        WHEN b.best_e1rm_squat > 0 AND b.best_e1rm_bench > 0 AND b.best_e1rm_deadlift > 0 AND b.poids_corps_moyen_kg > 0
        THEN powertrack_calc_dots(b.best_e1rm_squat + b.best_e1rm_bench + b.best_e1rm_deadlift, b.poids_corps_moyen_kg)
        ELSE NULL
    END AS dots,
    CASE
        WHEN b.best_e1rm_squat > 0 AND b.best_e1rm_bench > 0 AND b.best_e1rm_deadlift > 0 AND b.poids_corps_moyen_kg > 0
        THEN powertrack_calc_ipf_gl(b.best_e1rm_squat + b.best_e1rm_bench + b.best_e1rm_deadlift, b.poids_corps_moyen_kg)
        ELSE NULL
    END AS ipf_gl
FROM base b
ORDER BY b.semaine_debut DESC, b.semaine_numero DESC NULLS LAST;

CREATE OR REPLACE VIEW v_recuperation_glissante AS
WITH ref AS (
    SELECT COALESCE(MAX(date_seance), NOW()) AS ref_date FROM seances
),
latest_bw AS (
    SELECT r.poids_corps_kg
    FROM ressentis_seances r
    JOIN seances s ON s.seance_id = r.seance_id
    WHERE r.poids_corps_kg IS NOT NULL
    ORDER BY s.date_seance DESC
    LIMIT 1
)
SELECT
    (SELECT poids_corps_kg FROM latest_bw) AS dernier_poids_corps_kg,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '7 days' THEN v.poids_corps_kg END)::NUMERIC, 2) AS poids_corps_7j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '14 days' THEN v.poids_corps_kg END)::NUMERIC, 2) AS poids_corps_14j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '7 days' THEN v.sommeil_heures END)::NUMERIC, 2) AS sommeil_7j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '14 days' THEN v.sommeil_heures END)::NUMERIC, 2) AS sommeil_14j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '7 days' THEN v.hydratation_litres END)::NUMERIC, 2) AS hydratation_7j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '14 days' THEN v.hydratation_litres END)::NUMERIC, 2) AS hydratation_14j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '7 days' THEN v.fatigue_lombaire END)::NUMERIC, 2) AS fatigue_lombaire_7j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '14 days' THEN v.fatigue_lombaire END)::NUMERIC, 2) AS fatigue_lombaire_14j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '7 days' THEN v.stress END)::NUMERIC, 2) AS stress_7j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '14 days' THEN v.stress END)::NUMERIC, 2) AS stress_14j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '7 days' THEN v.duree_minutes END)::NUMERIC, 1) AS duree_moyenne_7j,
    ROUND(AVG(CASE WHEN v.date_seance >= ref.ref_date - INTERVAL '14 days' THEN v.duree_minutes END)::NUMERIC, 1) AS duree_moyenne_14j
FROM v_seances_synthese v
CROSS JOIN ref;

CREATE OR REPLACE VIEW v_alertes_fatigue AS
WITH e1rm_series AS (
    SELECT
        s.seance_id,
        s.date_seance,
        se.mouvement_parent,
        MAX(COALESCE(se.e1rm, powertrack_calc_e1rm(se.charge, se.reps_cibles, COALESCE(se.rpe, se.rpe_cible)))) AS session_e1rm
    FROM series_effectuees se
    JOIN seances s ON s.seance_id = se.seance_id
    WHERE se.est_validee = TRUE
      AND se.mouvement_parent IN ('squat', 'bench', 'deadlift')
    GROUP BY s.seance_id, s.date_seance, se.mouvement_parent
),
e1rm_drop AS (
    SELECT
        cur.seance_id,
        cur.date_seance,
        cur.mouvement_parent,
        cur.session_e1rm,
        MAX(prev.session_e1rm) AS peak_28j_precedent,
        ROUND((((MAX(prev.session_e1rm) - cur.session_e1rm) / NULLIF(MAX(prev.session_e1rm), 0)) * 100.0)::NUMERIC, 1) AS baisse_pct
    FROM e1rm_series cur
    JOIN e1rm_series prev
      ON prev.mouvement_parent = cur.mouvement_parent
     AND prev.date_seance < cur.date_seance
     AND prev.date_seance >= cur.date_seance - INTERVAL '28 days'
    GROUP BY cur.seance_id, cur.date_seance, cur.mouvement_parent, cur.session_e1rm
    HAVING cur.session_e1rm <= MAX(prev.session_e1rm) * 0.96
)
SELECT
    v.seance_id,
    v.date_seance,
    v.seance_titre,
    'DUREE_CRITIQUE'::TEXT AS code_alerte,
    'critique'::TEXT AS severite,
    FORMAT('Séance de %s min (> 120 min max)', v.duree_minutes) AS detail
FROM v_seances_synthese v
WHERE v.duree_minutes > 120

UNION ALL
SELECT
    v.seance_id,
    v.date_seance,
    v.seance_titre,
    'DUREE_ELEVEE',
    'warning',
    FORMAT('Séance de %s min (cible <= 95 min, seuil 105 min)', v.duree_minutes)
FROM v_seances_synthese v
WHERE v.duree_minutes > 105 AND v.duree_minutes <= 120

UNION ALL
SELECT
    v.seance_id,
    v.date_seance,
    v.seance_titre,
    'OVERSHOOT_RPE',
    'warning',
    FORMAT('%s série(s) à deltaRPE >= +1.0 (max +%s)', v.overshoots_count, v.delta_rpe_max)
FROM v_seances_synthese v
WHERE v.overshoots_count > 0

UNION ALL
SELECT
    v.seance_id,
    v.date_seance,
    v.seance_titre,
    'SERIES_NON_VALIDEES',
    'warning',
    FORMAT('%s série(s) programmée(s) non validée(s)', v.series_non_validees_count)
FROM v_seances_synthese v
WHERE v.series_non_validees_count > 0

UNION ALL
SELECT
    v.seance_id,
    v.date_seance,
    v.seance_titre,
    'FATIGUE_LOMBAIRE_HAUTE',
    'critique',
    FORMAT('Fatigue lombaire %s/5', v.fatigue_lombaire)
FROM v_seances_synthese v
WHERE v.fatigue_lombaire >= 4

UNION ALL
SELECT
    v.seance_id,
    v.date_seance,
    v.seance_titre,
    'STRESS_ELEVE',
    'warning',
    FORMAT('Stress élevé %s/5', v.stress)
FROM v_seances_synthese v
WHERE v.stress >= 4

UNION ALL
SELECT
    v.seance_id,
    v.date_seance,
    v.seance_titre,
    'HYDRATATION_INSUFFISANTE',
    'warning',
    FORMAT('Hydratation %s L (< 2.0 L)', v.hydratation_litres)
FROM v_seances_synthese v
WHERE v.hydratation_litres < 2.0

UNION ALL
SELECT
    v.seance_id,
    v.date_seance,
    v.seance_titre,
    'SOMMEIL_BAS',
    'warning',
    FORMAT('Sommeil %s h (< 6.5 h)', v.sommeil_heures)
FROM v_seances_synthese v
WHERE v.sommeil_heures < 6.5

UNION ALL
SELECT
    v.seance_id,
    v.date_seance,
    v.seance_titre,
    'DOULEUR_SIGNALEE',
    'critique',
    FORMAT('Douleur signalée : %s', v.douleur_signalee)
FROM v_seances_synthese v
WHERE v.douleur_active = TRUE

UNION ALL
SELECT
    d.seance_id,
    d.date_seance,
    s.seance_titre,
    'BAISSE_E1RM',
    'critique',
    FORMAT('Baisse e1RM %s de %s%% (%s kg vs pic 28j %s kg)', d.mouvement_parent, d.baisse_pct, d.session_e1rm, d.peak_28j_precedent)
FROM e1rm_drop d
JOIN seances s ON s.seance_id = d.seance_id;

-- -----------------------------------------------------------------------------
-- 4. Atomic Idempotent Upsert Procedure (called by n8n POST /webhook/powertrack)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION powertrack_upsert_seance(p_payload JSONB)
RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_seance_id TEXT;
    v_schema_version INT;
    v_ressenti JSONB;
    v_ex JSONB;
    v_ser JSONB;
    v_ex_idx INT := 0;
    v_ser_idx INT := 0;
    v_series_inserted INT := 0;
    v_parent TEXT;
    v_charge NUMERIC(6,2);
    v_reps INT;
    v_rpe_cible NUMERIC(3,1);
    v_rpe NUMERIC(3,1);
    v_delta NUMERIC(4,1);
    v_e1rm NUMERIC(6,2);
    v_validee BOOLEAN;
    v_alertes JSONB;
    v_synthese JSONB;
BEGIN
    IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'Payload JSON invalide (objet attendu)';
    END IF;

    v_schema_version := COALESCE((p_payload->>'schemaVersion')::INT, 2);
    v_seance_id := NULLIF(BTRIM(p_payload->>'seanceId'), '');
    IF v_seance_id IS NULL THEN
        RAISE EXCEPTION 'Champ obligatoire manquant: seanceId';
    END IF;

    INSERT INTO seances (
        seance_id,
        schema_version,
        export_date,
        date_seance,
        bloc_nom,
        bloc_date_debut,
        bloc_squat_1rm,
        bloc_bench_1rm,
        bloc_deadlift_1rm,
        semaine_numero,
        semaine_nom,
        seance_titre,
        heure_debut,
        duree_minutes,
        series_validees_count,
        series_programmees_count,
        taux_completion,
        tonnage_total_kg,
        raw_payload,
        updated_at
    ) VALUES (
        v_seance_id,
        v_schema_version,
        COALESCE((p_payload->>'exportDate')::TIMESTAMPTZ, NOW()),
        (p_payload->>'dateSeance')::TIMESTAMPTZ,
        NULLIF(p_payload->>'blocNom', ''),
        NULLIF(p_payload->>'blocDateDebut', '')::TIMESTAMPTZ,
        (p_payload->'bloc1RM'->>'squat')::NUMERIC(6,2),
        (p_payload->'bloc1RM'->>'bench')::NUMERIC(6,2),
        (p_payload->'bloc1RM'->>'deadlift')::NUMERIC(6,2),
        (p_payload->>'semaineNumero')::INT,
        COALESCE(NULLIF(p_payload->>'semaineNom', ''), 'Semaine inconnue'),
        NULLIF(p_payload->>'seanceTitre', ''),
        NULLIF(p_payload->>'heureDebut', '')::TIMESTAMPTZ,
        (p_payload->>'dureeMinutes')::INT,
        COALESCE((p_payload->>'seriesValideesCount')::INT, 0),
        COALESCE((p_payload->>'seriesProgrammeesCount')::INT, 0),
        COALESCE((p_payload->>'tauxCompletion')::NUMERIC(5,3), 0),
        COALESCE((p_payload->>'tonnageTotalKg')::NUMERIC(10,2), 0),
        p_payload,
        NOW()
    )
    ON CONFLICT (seance_id) DO UPDATE SET
        schema_version = EXCLUDED.schema_version,
        export_date = EXCLUDED.export_date,
        date_seance = EXCLUDED.date_seance,
        bloc_nom = EXCLUDED.bloc_nom,
        bloc_date_debut = EXCLUDED.bloc_date_debut,
        bloc_squat_1rm = EXCLUDED.bloc_squat_1rm,
        bloc_bench_1rm = EXCLUDED.bloc_bench_1rm,
        bloc_deadlift_1rm = EXCLUDED.bloc_deadlift_1rm,
        semaine_numero = EXCLUDED.semaine_numero,
        semaine_nom = EXCLUDED.semaine_nom,
        seance_titre = EXCLUDED.seance_titre,
        heure_debut = EXCLUDED.heure_debut,
        duree_minutes = EXCLUDED.duree_minutes,
        series_validees_count = EXCLUDED.series_validees_count,
        series_programmees_count = EXCLUDED.series_programmees_count,
        taux_completion = EXCLUDED.taux_completion,
        tonnage_total_kg = EXCLUDED.tonnage_total_kg,
        raw_payload = EXCLUDED.raw_payload,
        updated_at = NOW();

    v_ressenti := p_payload->'ressenti';
    IF v_ressenti IS NOT NULL AND jsonb_typeof(v_ressenti) = 'object' THEN
        INSERT INTO ressentis_seances (
            seance_id,
            poids_corps_kg,
            sommeil_heures,
            niveau_energie,
            fatigue_lombaire,
            stress,
            hydratation_litres,
            douleur_signalee,
            notes_seance
        ) VALUES (
            v_seance_id,
            (v_ressenti->>'poidsCorpsKg')::NUMERIC(5,2),
            (v_ressenti->>'sommeilHeures')::NUMERIC(4,2),
            (v_ressenti->>'niveauEnergie')::INT,
            (v_ressenti->>'fatigueLombaire')::INT,
            (v_ressenti->>'stress')::INT,
            (v_ressenti->>'hydratationLitres')::NUMERIC(4,2),
            NULLIF(BTRIM(v_ressenti->>'douleurSignalee'), ''),
            NULLIF(BTRIM(v_ressenti->>'notesSeance'), '')
        )
        ON CONFLICT (seance_id) DO UPDATE SET
            poids_corps_kg = EXCLUDED.poids_corps_kg,
            sommeil_heures = EXCLUDED.sommeil_heures,
            niveau_energie = EXCLUDED.niveau_energie,
            fatigue_lombaire = EXCLUDED.fatigue_lombaire,
            stress = EXCLUDED.stress,
            hydratation_litres = EXCLUDED.hydratation_litres,
            douleur_signalee = EXCLUDED.douleur_signalee,
            notes_seance = EXCLUDED.notes_seance;
    ELSE
        DELETE FROM ressentis_seances WHERE seance_id = v_seance_id;
    END IF;

    DELETE FROM series_effectuees WHERE seance_id = v_seance_id;

    v_ex_idx := 0;
    FOR v_ex IN SELECT value FROM jsonb_array_elements(COALESCE(p_payload->'exercices', '[]'::JSONB)) LOOP
        v_parent := powertrack_infer_parent(v_ex->>'nom', v_ex->>'mouvementParent');
        v_ser_idx := 0;
        FOR v_ser IN SELECT value FROM jsonb_array_elements(COALESCE(v_ex->'series', '[]'::JSONB)) LOOP
            v_ser_idx := v_ser_idx + 1;
            v_reps := (v_ser->>'repsCibles')::INT;
            v_rpe_cible := (v_ser->>'rpeCible')::NUMERIC(3,1);
            v_charge := (v_ser->>'charge')::NUMERIC(6,2);
            v_rpe := (v_ser->>'rpe')::NUMERIC(3,1);
            v_validee := COALESCE((v_ser->>'estValidee')::BOOLEAN, FALSE);

            IF v_ser->>'deltaRPE' IS NOT NULL THEN
                v_delta := (v_ser->>'deltaRPE')::NUMERIC(4,1);
            ELSIF v_validee AND v_rpe IS NOT NULL THEN
                v_delta := ROUND((v_rpe - v_rpe_cible)::NUMERIC, 1);
            ELSE
                v_delta := NULL;
            END IF;

            IF v_ser->>'e1RM' IS NOT NULL THEN
                v_e1rm := (v_ser->>'e1RM')::NUMERIC(6,2);
            ELSIF v_validee AND v_parent IN ('squat', 'bench', 'deadlift') THEN
                v_e1rm := powertrack_calc_e1rm(v_charge, v_reps, COALESCE(v_rpe, v_rpe_cible));
            ELSE
                v_e1rm := NULL;
            END IF;

            INSERT INTO series_effectuees (
                seance_id,
                exercice_ordre,
                exercice_nom,
                categorie,
                mouvement_parent,
                indications,
                serie_numero,
                reps_cibles,
                rpe_cible,
                charge,
                rpe,
                delta_rpe,
                e1rm,
                est_validee
            ) VALUES (
                v_seance_id,
                COALESCE((v_ex->>'ordre')::INT, v_ex_idx),
                v_ex->>'nom',
                COALESCE(v_ex->>'categorie', 'polyarticulaire'),
                v_parent,
                NULLIF(v_ex->>'indications', ''),
                COALESCE((v_ser->>'numero')::INT, v_ser_idx),
                v_reps,
                v_rpe_cible,
                v_charge,
                v_rpe,
                v_delta,
                v_e1rm,
                v_validee
            );

            v_series_inserted := v_series_inserted + 1;
        END LOOP;
        v_ex_idx := v_ex_idx + 1;
    END LOOP;

    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'code', code_alerte,
        'severite', severite,
        'detail', detail
    )), '[]'::JSONB)
    INTO v_alertes
    FROM v_alertes_fatigue
    WHERE seance_id = v_seance_id;

    SELECT to_jsonb(v) INTO v_synthese FROM v_seances_synthese v WHERE v.seance_id = v_seance_id;

    RETURN jsonb_build_object(
        'status', 'saved',
        'seanceId', v_seance_id,
        'seriesInserted', v_series_inserted,
        'alertes', v_alertes,
        'syntheseSeance', v_synthese
    );
END;
$$;

-- -----------------------------------------------------------------------------
-- 5. Coach Summary Function (called by n8n GET /webhook/powertrack/coach-summary)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION powertrack_coach_summary(
    p_limit INT DEFAULT 12,
    p_weeks INT DEFAULT NULL,
    p_bloc TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_limit INT := LEAST(GREATEST(COALESCE(p_limit, 12), 1), 100);
    v_bloc TEXT := NULLIF(BTRIM(p_bloc), '');
    v_ref_date TIMESTAMPTZ;
    v_seances_raw JSONB;
    v_hebdo JSONB;
    v_variantes JSONB;
    v_douleurs_30j JSONB;
    v_alertes JSONB;
    v_recup JSONB;
    v_bw NUMERIC;
    v_windows JSONB := '{}'::JSONB;
    v_win_label TEXT;
    v_win_days INT;
    v_sq NUMERIC;
    v_bn NUMERIC;
    v_dl NUMERIC;
    v_tot NUMERIC;
BEGIN
    SELECT COALESCE(MAX(date_seance), NOW()) INTO v_ref_date FROM seances;

    -- 1) seancesRaw: N latest raw_payload compatible with analyze_powertrack.py
    WITH filtered AS (
        SELECT s.seance_id, s.date_seance, s.raw_payload
        FROM seances s
        WHERE (v_bloc IS NULL OR s.bloc_nom = v_bloc)
          AND (p_weeks IS NULL OR s.date_seance >= v_ref_date - make_interval(weeks => p_weeks))
        ORDER BY s.date_seance DESC
        LIMIT v_limit
    )
    SELECT COALESCE(jsonb_agg(raw_payload ORDER BY date_seance DESC), '[]'::JSONB)
    INTO v_seances_raw
    FROM filtered;

    -- 2) Weekly synthesis over filtered sessions
    WITH filtered_ids AS (
        SELECT s.seance_id
        FROM seances s
        WHERE (v_bloc IS NULL OR s.bloc_nom = v_bloc)
          AND (p_weeks IS NULL OR s.date_seance >= v_ref_date - make_interval(weeks => p_weeks))
        ORDER BY s.date_seance DESC
        LIMIT v_limit
    ),
    wk AS (
        SELECT
            (DATE_TRUNC('week', v.date_seance AT TIME ZONE 'Europe/Paris'))::DATE AS semaine_debut,
            v.bloc_nom,
            v.semaine_numero,
            v.semaine_nom,
            COUNT(*)::INT AS nb_seances,
            ROUND(SUM(v.tonnage_total_kg)::NUMERIC, 1) AS tonnage_total_kg,
            ROUND(SUM(v.tonnage_squat_kg)::NUMERIC, 1) AS tonnage_squat_kg,
            ROUND(SUM(v.tonnage_bench_kg)::NUMERIC, 1) AS tonnage_bench_kg,
            ROUND(SUM(v.tonnage_deadlift_kg)::NUMERIC, 1) AS tonnage_deadlift_kg,
            ROUND(SUM(v.tonnage_autre_kg)::NUMERIC, 1) AS tonnage_autre_kg,
            ROUND(SUM(v.tonnage_competition_kg)::NUMERIC, 1) AS tonnage_competition_kg,
            ROUND(SUM(v.tonnage_polyarticulaire_kg)::NUMERIC, 1) AS tonnage_polyarticulaire_kg,
            ROUND(SUM(v.tonnage_isolation_kg)::NUMERIC, 1) AS tonnage_isolation_kg,
            ROUND(AVG(v.delta_rpe_moyen)::NUMERIC, 2) AS delta_rpe_moyen,
            ROUND(MAX(v.delta_rpe_max)::NUMERIC, 1) AS delta_rpe_max,
            SUM(v.overshoots_count)::INT AS overshoots_count,
            SUM(v.series_non_validees_count)::INT AS series_non_validees_count,
            SUM(v.series_axiales_sbd)::INT AS series_axiales_hebdo,
            ROUND(AVG(v.duree_minutes)::NUMERIC, 1) AS duree_moyenne_min,
            ROUND(AVG(v.poids_corps_kg)::NUMERIC, 2) AS poids_corps_moyen_kg,
            ROUND(AVG(v.sommeil_heures)::NUMERIC, 2) AS sommeil_moyen_h,
            ROUND(AVG(v.hydratation_litres)::NUMERIC, 2) AS hydratation_moyenne_l,
            ROUND(AVG(v.fatigue_lombaire)::NUMERIC, 2) AS fatigue_lombaire_moyenne,
            ROUND(AVG(v.stress)::NUMERIC, 2) AS stress_moyen,
            MAX(v.best_e1rm_squat) AS best_e1rm_squat,
            MAX(v.best_e1rm_bench) AS best_e1rm_bench,
            MAX(v.best_e1rm_deadlift) AS best_e1rm_deadlift
        FROM v_seances_synthese v
        JOIN filtered_ids f ON f.seance_id = v.seance_id
        GROUP BY
            (DATE_TRUNC('week', v.date_seance AT TIME ZONE 'Europe/Paris'))::DATE,
            v.bloc_nom,
            v.semaine_numero,
            v.semaine_nom
        ORDER BY semaine_debut DESC, v.semaine_numero DESC NULLS LAST
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'semaineDebut', wk.semaine_debut,
        'blocNom', wk.bloc_nom,
        'semaineNumero', wk.semaine_numero,
        'semaineNom', wk.semaine_nom,
        'nbSeances', wk.nb_seances,
        'tonnageTotalKg', wk.tonnage_total_kg,
        'tonnageParMouvement', jsonb_build_object(
            'squat', wk.tonnage_squat_kg,
            'bench', wk.tonnage_bench_kg,
            'deadlift', wk.tonnage_deadlift_kg,
            'autre', wk.tonnage_autre_kg
        ),
        'tonnageParCategorie', jsonb_build_object(
            'competition', wk.tonnage_competition_kg,
            'polyarticulaire', wk.tonnage_polyarticulaire_kg,
            'isolation', wk.tonnage_isolation_kg
        ),
        'deltaRPEMoyen', wk.delta_rpe_moyen,
        'deltaRPEMax', wk.delta_rpe_max,
        'overshootsCount', wk.overshoots_count,
        'seriesNonValideesCount', wk.series_non_validees_count,
        'seriesAxialesHebdo', wk.series_axiales_hebdo,
        'dureeMoyenneMinutes', wk.duree_moyenne_min,
        'recuperation', jsonb_build_object(
            'poidsCorpsMoyenKg', wk.poids_corps_moyen_kg,
            'sommeilMoyenHeures', wk.sommeil_moyen_h,
            'hydratationMoyenneLitres', wk.hydratation_moyenne_l,
            'fatigueLombaireMoyenne', wk.fatigue_lombaire_moyenne,
            'stressMoyen', wk.stress_moyen
        ),
        'meilleursE1RM', jsonb_build_object(
            'squat', wk.best_e1rm_squat,
            'bench', wk.best_e1rm_bench,
            'deadlift', wk.best_e1rm_deadlift
        ),
        'totalEstimeKg', CASE WHEN wk.best_e1rm_squat > 0 AND wk.best_e1rm_bench > 0 AND wk.best_e1rm_deadlift > 0
            THEN ROUND((wk.best_e1rm_squat + wk.best_e1rm_bench + wk.best_e1rm_deadlift)::NUMERIC, 1) ELSE NULL END,
        'dots', CASE WHEN wk.best_e1rm_squat > 0 AND wk.best_e1rm_bench > 0 AND wk.best_e1rm_deadlift > 0 AND wk.poids_corps_moyen_kg > 0
            THEN powertrack_calc_dots(wk.best_e1rm_squat + wk.best_e1rm_bench + wk.best_e1rm_deadlift, wk.poids_corps_moyen_kg) ELSE NULL END,
        'ipfGL', CASE WHEN wk.best_e1rm_squat > 0 AND wk.best_e1rm_bench > 0 AND wk.best_e1rm_deadlift > 0 AND wk.poids_corps_moyen_kg > 0
            THEN powertrack_calc_ipf_gl(wk.best_e1rm_squat + wk.best_e1rm_bench + wk.best_e1rm_deadlift, wk.poids_corps_moyen_kg) ELSE NULL END
    )), '[]'::JSONB)
    INTO v_hebdo
    FROM wk;

    -- 3) Latest bodyweight
    SELECT r.poids_corps_kg
    INTO v_bw
    FROM ressentis_seances r
    JOIN seances s ON s.seance_id = r.seance_id
    WHERE r.poids_corps_kg IS NOT NULL
      AND (v_bloc IS NULL OR s.bloc_nom = v_bloc)
    ORDER BY s.date_seance DESC
    LIMIT 1;

    -- 4) Rolling SBD progression (7j, 14j, 28j, global)
    FOR v_win_label, v_win_days IN
        SELECT * FROM (VALUES ('7j', 7), ('14j', 14), ('28j', 28), ('global', NULL::INT)) AS w(lbl, d)
    LOOP
        SELECT
            MAX(CASE WHEN se.mouvement_parent = 'squat'
                THEN COALESCE(se.e1rm, powertrack_calc_e1rm(se.charge, se.reps_cibles, COALESCE(se.rpe, se.rpe_cible))) END),
            MAX(CASE WHEN se.mouvement_parent = 'bench'
                THEN COALESCE(se.e1rm, powertrack_calc_e1rm(se.charge, se.reps_cibles, COALESCE(se.rpe, se.rpe_cible))) END),
            MAX(CASE WHEN se.mouvement_parent = 'deadlift'
                THEN COALESCE(se.e1rm, powertrack_calc_e1rm(se.charge, se.reps_cibles, COALESCE(se.rpe, se.rpe_cible))) END)
        INTO v_sq, v_bn, v_dl
        FROM series_effectuees se
        JOIN seances s ON s.seance_id = se.seance_id
        WHERE se.est_validee = TRUE
          AND (v_bloc IS NULL OR s.bloc_nom = v_bloc)
          AND (v_win_days IS NULL OR s.date_seance >= v_ref_date - make_interval(days => v_win_days));

        IF COALESCE(v_sq, 0) > 0 AND COALESCE(v_bn, 0) > 0 AND COALESCE(v_dl, 0) > 0 THEN
            v_tot := ROUND((v_sq + v_bn + v_dl)::NUMERIC, 1);
        ELSE
            v_tot := NULL;
        END IF;

        v_windows := v_windows || jsonb_build_object(
            v_win_label,
            jsonb_build_object(
                'squat', v_sq,
                'bench', v_bn,
                'deadlift', v_dl,
                'totalEstimeKg', v_tot,
                'repartitionSBDPourcent', CASE WHEN v_tot IS NOT NULL AND v_tot > 0 THEN jsonb_build_object(
                    'squat', ROUND((v_sq / v_tot * 100.0)::NUMERIC, 1),
                    'bench', ROUND((v_bn / v_tot * 100.0)::NUMERIC, 1),
                    'deadlift', ROUND((v_dl / v_tot * 100.0)::NUMERIC, 1)
                ) ELSE NULL END,
                'poidsCorpsKg', v_bw,
                'dots', CASE WHEN v_tot IS NOT NULL AND v_bw > 0 THEN powertrack_calc_dots(v_tot, v_bw) ELSE NULL END,
                'ipfGL', CASE WHEN v_tot IS NOT NULL AND v_bw > 0 THEN powertrack_calc_ipf_gl(v_tot, v_bw) ELSE NULL END,
                'ffforce', CASE WHEN v_tot IS NOT NULL THEN powertrack_ffforce_status(v_tot, v_bw) ELSE NULL END
            )
        );
    END LOOP;

    -- 5) Per-variant records (filtered by bloc if requested)
    IF v_bloc IS NOT NULL THEN
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'mouvementParent', rb.mouvement_parent,
            'exerciceNom', rb.exercice_nom,
            'bestE1RM', rb.best_e1rm,
            'deltaRPEMoyen', rb.delta_rpe_moyen,
            'deltaRPEMax', rb.delta_rpe_max,
            'overshootsCount', rb.overshoots_count,
            'seriesNonValideesCount', rb.series_non_validees_count,
            'seriesValideesCount', rb.series_validees_count
        ) ORDER BY rb.mouvement_parent, rb.best_e1rm DESC NULLS LAST), '[]'::JSONB)
        INTO v_variantes
        FROM v_records_par_bloc rb
        WHERE rb.bloc_nom = v_bloc;
    ELSE
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'mouvementParent', r.mouvement_parent,
            'exerciceNom', r.exercice_nom,
            'bestE1RM7j', r.best_e1rm_7j,
            'bestE1RM14j', r.best_e1rm_14j,
            'bestE1RM28j', r.best_e1rm_28j,
            'bestE1RMGlobal', r.best_e1rm_global,
            'deltaRPEMoyen', r.delta_rpe_moyen,
            'deltaRPEMax', r.delta_rpe_max,
            'overshootsCount', r.overshoots_count,
            'seriesValideesCount', r.series_validees_count,
            'derniereSeance', r.derniere_seance
        ) ORDER BY r.mouvement_parent, r.best_e1rm_global DESC NULLS LAST), '[]'::JSONB)
        INTO v_variantes
        FROM v_records_mouvements r;
    END IF;

    -- 6) Rolling recovery
    SELECT to_jsonb(rec) INTO v_recup FROM v_recuperation_glissante rec;

    -- 7) Pain reports in the last 30 days (relative to latest session)
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'seanceId', v.seance_id,
        'dateSeance', v.date_seance,
        'seanceTitre', v.seance_titre,
        'douleurSignalee', v.douleur_signalee,
        'notesSeance', v.notes_seance
    ) ORDER BY v.date_seance DESC), '[]'::JSONB)
    INTO v_douleurs_30j
    FROM v_seances_synthese v
    WHERE v.douleur_active = TRUE
      AND v.date_seance >= v_ref_date - INTERVAL '30 days'
      AND (v_bloc IS NULL OR v.bloc_nom = v_bloc);

    -- 8) Active alerts over filtered sessions + recurring pattern alerts
    WITH filtered_ids AS (
        SELECT s.seance_id
        FROM seances s
        WHERE (v_bloc IS NULL OR s.bloc_nom = v_bloc)
          AND (p_weeks IS NULL OR s.date_seance >= v_ref_date - make_interval(weeks => p_weeks))
        ORDER BY s.date_seance DESC
        LIMIT v_limit
    ),
    session_alerts AS (
        SELECT
            a.seance_id,
            a.date_seance,
            a.seance_titre,
            a.code_alerte,
            a.severite,
            a.detail
        FROM v_alertes_fatigue a
        JOIN filtered_ids f ON f.seance_id = a.seance_id
    ),
    recurring_alerts AS (
        SELECT
            NULL::TEXT AS seance_id,
            MAX(v.date_seance) AS date_seance,
            NULL::TEXT AS seance_titre,
            'OVERSHOOTS_REPETES'::TEXT AS code_alerte,
            'critique'::TEXT AS severite,
            FORMAT('%s séances avec overshoot RPE >= +1.0 sur la période', COUNT(*)) AS detail
        FROM v_seances_synthese v
        JOIN filtered_ids f ON f.seance_id = v.seance_id
        WHERE v.overshoots_count > 0
        HAVING COUNT(*) >= 2

        UNION ALL
        SELECT
            NULL::TEXT,
            MAX(v.date_seance),
            NULL::TEXT,
            'HYDRATATION_RECURRENTE',
            'critique',
            FORMAT('%s séances avec hydratation < 2.0 L sur la période', COUNT(*))
        FROM v_seances_synthese v
        JOIN filtered_ids f ON f.seance_id = v.seance_id
        WHERE v.hydratation_litres < 2.0
        HAVING COUNT(*) >= 2
    ),
    all_alerts AS (
        SELECT * FROM recurring_alerts
        UNION ALL
        SELECT * FROM session_alerts
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'seanceId', al.seance_id,
        'dateSeance', al.date_seance,
        'seanceTitre', al.seance_titre,
        'code', al.code_alerte,
        'severite', al.severite,
        'detail', al.detail
    ) ORDER BY al.date_seance DESC NULLS LAST), '[]'::JSONB)
    INTO v_alertes
    FROM all_alerts al;

    RETURN jsonb_build_object(
        'seancesRaw', v_seances_raw,
        'syntheseServeur', jsonb_build_object(
            'filtre', jsonb_build_object('limit', v_limit, 'weeks', p_weeks, 'bloc', v_bloc),
            'hebdo', v_hebdo,
            'progressionSBD', v_windows,
            'parVariante', v_variantes,
            'recuperationGlissante', v_recup,
            'douleurs30Jours', v_douleurs_30j
        ),
        'alertesActives', v_alertes
    );
END;
$$;
