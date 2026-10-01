-- Grant the application role the read-only ACL data required for the Go
-- authorization boundary. The migration role remains the only migration
-- executor; this does not grant ACL mutation or identity mutation rights.
BEGIN;
SELECT pg_advisory_xact_lock(742018);

GRANT SELECT ON acl, permission_revision TO rag_app;

INSERT INTO schema_migration(version, checksum_sha256)
VALUES ('0007', :'migration_checksum') ON CONFLICT (version) DO NOTHING;

COMMIT;
