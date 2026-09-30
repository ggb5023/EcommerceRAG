-- Record checksums for migrations introduced after the legacy bookkeeping table.
BEGIN;
SELECT pg_advisory_xact_lock(742018);
ALTER TABLE schema_migration ADD COLUMN IF NOT EXISTS checksum_sha256 TEXT;
ALTER TABLE schema_migration DROP CONSTRAINT IF EXISTS schema_migration_checksum_format;
ALTER TABLE schema_migration ADD CONSTRAINT schema_migration_checksum_format
    CHECK (checksum_sha256 IS NULL OR checksum_sha256 ~ '^[0-9a-f]{64}$');
INSERT INTO schema_migration(version, checksum_sha256)
VALUES ('0004', :'migration_checksum') ON CONFLICT (version) DO NOTHING;
COMMIT;
