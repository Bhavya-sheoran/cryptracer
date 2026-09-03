-- ===========================================================================
-- SIH26183 : service exposure results.
--
-- Applied two ways, and idempotently in both, because a demo database already
-- exists and docker-entrypoint-initdb.d only runs on an empty volume:
--   * mounted into initdb, so a fresh volume gets it at creation
--   * applied by the backend at startup (app/db/postgres.py), so an existing
--     volume gets it without being wiped
--
-- Why persist an exposure at all: it is a finding an officer may act on. If a
-- freeze request cites "Meridian Exchange, 78% of traced funds", that claim has
-- to be reconstructable months later - including the weights and price basis
-- that produced it, which will have moved on by then.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS service_exposures (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chain              chain_t NOT NULL,
    address_norm       TEXT NOT NULL,
    case_id            UUID REFERENCES cases(id) ON DELETE SET NULL,

    -- direct | indirect | none
    kind               TEXT NOT NULL,
    searched_to_hop    INTEGER NOT NULL,

    -- Denormalised top result, so a listing does not need to join paths.
    top_service        TEXT,
    top_service_type   TEXT,
    top_hop            INTEGER,
    top_score          NUMERIC(6,4),
    top_volume_inr     NUMERIC(20,2),

    -- Reproducibility: the exact weights and version behind the numbers above.
    scoring_version    TEXT NOT NULL,
    weights            JSONB NOT NULL DEFAULT '{}'::jsonb,
    explanation        TEXT,

    computed_by        UUID REFERENCES users(id),
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_exposure_address
    ON service_exposures (chain, address_norm, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_exposure_service
    ON service_exposures (top_service);
CREATE INDEX IF NOT EXISTS idx_exposure_case
    ON service_exposures (case_id);

-- One row per candidate service, carrying the measurements AND the per-feature
-- arithmetic. The explanation is stored rather than recomputed: recomputing it
-- later against different weights would silently rewrite history.
CREATE TABLE IF NOT EXISTS exposure_paths (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    exposure_id        UUID NOT NULL REFERENCES service_exposures(id) ON DELETE CASCADE,

    rank               INTEGER NOT NULL,
    service            TEXT NOT NULL,
    service_type       TEXT,
    service_address    TEXT,
    hop                INTEGER NOT NULL,
    score              NUMERIC(6,4),

    total_volume       NUMERIC(38,18),
    total_volume_inr   NUMERIC(20,2),
    volume_basis       TEXT,
    transfer_count     INTEGER,
    unique_counterparties INTEGER,
    continuity_ok      BOOLEAN,
    last_seen          TIMESTAMPTZ,

    label_confidence   REAL,
    label_sources      TEXT[],
    price_sources      TEXT[],

    path               TEXT[],
    features           JSONB NOT NULL DEFAULT '{}'::jsonb,
    explanation        JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Only populated for a direct exposure, where a single transaction is the
    -- whole finding.
    evidence_txid      TEXT,
    evidence_amount    NUMERIC(38,18),
    evidence_asset     TEXT,
    evidence_timestamp TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_exposure_paths_exposure
    ON exposure_paths (exposure_id, rank);
