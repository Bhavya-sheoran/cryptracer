-- Hash chain over the audit log.
--
-- The record of who approved a freeze sat in an ordinary table: editable,
-- deletable, and forgeable without trace. These two columns let every entry
-- commit to its predecessor, so tampering breaks the chain and is detectable
-- even though it is not prevented.
--
-- Nullable on purpose. Entries written before this existed have no hashes, and
-- backfilling them would be worse than leaving them empty: a computed hash on
-- a historical row would assert that it had been verified when it had not.
-- verify_chain() reports them as unverifiable, which is the truth.

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS prev_hash  TEXT;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS entry_hash TEXT;

-- Verification walks the log in id order and reads the most recent hash to
-- extend the chain; both are covered by this.
CREATE INDEX IF NOT EXISTS idx_audit_log_entry_hash
    ON audit_log (id DESC) WHERE entry_hash IS NOT NULL;

COMMENT ON COLUMN audit_log.prev_hash IS
    'entry_hash of the preceding entry; 64 zeros for the first chained entry.';
COMMENT ON COLUMN audit_log.entry_hash IS
    'sha256(prev_hash || canonical form of this entry). NULL for entries predating chaining.';
