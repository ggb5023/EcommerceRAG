-- M1 clarification lineage. Keep 0001 and 0002 immutable.
BEGIN;
SELECT pg_advisory_xact_lock(742018);

ALTER TABLE turn ADD COLUMN IF NOT EXISTS parent_turn_id BIGINT;
ALTER TABLE turn DROP CONSTRAINT IF EXISTS turn_parent_turn_fk;
ALTER TABLE turn ADD CONSTRAINT turn_parent_turn_fk
    FOREIGN KEY (tenant_id, parent_turn_id) REFERENCES turn(tenant_id, id);
CREATE INDEX IF NOT EXISTS idx_turn_parent ON turn (tenant_id, parent_turn_id);

COMMIT;
