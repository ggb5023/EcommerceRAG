-- Synthetic authorization model. Development fixture only; no OIDC integration.
BEGIN;
SELECT pg_advisory_xact_lock(742018);

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS permission_revision TEXT NOT NULL DEFAULT 'auth-v1';
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS display_name TEXT;

CREATE TABLE IF NOT EXISTS permission_revision (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    revision TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_app_user_active_scope
    ON app_user (tenant_id, external_id) WHERE revoked_at IS NULL;

INSERT INTO schema_migration(version, checksum_sha256)
VALUES ('0005', :'migration_checksum') ON CONFLICT (version) DO NOTHING;

COMMIT;
