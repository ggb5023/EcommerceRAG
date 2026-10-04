BEGIN;
SELECT pg_advisory_xact_lock(742018);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_app') THEN
        RAISE EXCEPTION 'rag_app role is required for the admin status endpoint';
    END IF;
    GRANT SELECT ON schema_migration TO rag_app;
END $$;

INSERT INTO schema_migration(version,checksum_sha256)
VALUES ('0011', :'migration_checksum') ON CONFLICT(version) DO NOTHING;
COMMIT;
