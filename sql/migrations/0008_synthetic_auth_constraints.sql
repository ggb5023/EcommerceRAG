-- Tighten the synthetic authorization fixture at the database boundary.
-- This migration is intentionally separate from 0001-0007 so their historical
-- NULL checksums remain LEGACY_UNVERIFIED.
BEGIN;
SELECT pg_advisory_xact_lock(742018);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'app_user'::regclass AND conname = 'app_user_role_check'
    ) THEN
        ALTER TABLE app_user
            ADD CONSTRAINT app_user_role_check
            CHECK (role IN ('owner', 'admin', 'operator', 'viewer'));
    END IF;
END $$;

ALTER TABLE app_user ALTER COLUMN permission_revision SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'acl'::regclass AND conname = 'acl_resource_type_check'
    ) THEN
        ALTER TABLE acl
            ADD CONSTRAINT acl_resource_type_check
            CHECK (resource_type IN ('document', 'source', 'tenant'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'acl'::regclass AND conname = 'acl_subject_type_check'
    ) THEN
        ALTER TABLE acl
            ADD CONSTRAINT acl_subject_type_check
            CHECK (subject_type IN ('user', 'role'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'acl'::regclass AND conname = 'acl_permission_check'
    ) THEN
        ALTER TABLE acl
            ADD CONSTRAINT acl_permission_check
            CHECK (permission IN ('read', 'write', 'admin'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'acl'::regclass AND conname = 'acl_resource_id_check'
    ) THEN
        ALTER TABLE acl
            ADD CONSTRAINT acl_resource_id_check
            CHECK (resource_id > 0 AND length(btrim(subject_id)) > 0);
    END IF;
END $$;

ALTER TABLE acl
    DROP CONSTRAINT IF EXISTS acl_resource_type_resource_id_subject_type_subject_id_key;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'acl'::regclass AND conname = 'acl_tenant_resource_subject_key'
    ) THEN
        ALTER TABLE acl
            ADD CONSTRAINT acl_tenant_resource_subject_key
            UNIQUE (tenant_id, resource_type, resource_id, subject_type, subject_id);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION ecr_validate_acl_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    owner_tenant BIGINT;
    subject_tenant BIGINT;
BEGIN
    SELECT id INTO owner_tenant FROM tenant WHERE id = NEW.tenant_id;
    IF owner_tenant IS NULL THEN
        RAISE EXCEPTION 'ACL tenant does not exist' USING ERRCODE = '23503';
    END IF;

    IF NEW.resource_type = 'document' THEN
        SELECT tenant_id INTO owner_tenant FROM document WHERE id = NEW.resource_id;
    ELSIF NEW.resource_type = 'source' THEN
        SELECT tenant_id INTO owner_tenant FROM source WHERE id = NEW.resource_id;
    ELSE
        owner_tenant := NEW.resource_id;
    END IF;
    IF owner_tenant IS DISTINCT FROM NEW.tenant_id THEN
        RAISE EXCEPTION 'ACL resource belongs to another tenant or does not exist'
            USING ERRCODE = '23503';
    END IF;

    IF NEW.subject_type = 'user' THEN
        SELECT tenant_id INTO subject_tenant
        FROM app_user
        WHERE tenant_id = NEW.tenant_id
          AND (external_id = NEW.subject_id OR id::text = NEW.subject_id)
        LIMIT 1;
        IF subject_tenant IS DISTINCT FROM NEW.tenant_id THEN
            RAISE EXCEPTION 'ACL user subject belongs to another tenant or does not exist'
                USING ERRCODE = '23503';
        END IF;
    ELSIF NEW.subject_id NOT IN ('owner', 'admin', 'operator', 'viewer') THEN
        RAISE EXCEPTION 'ACL role subject is invalid' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_acl_scope ON acl;
CREATE TRIGGER trg_acl_scope
BEFORE INSERT OR UPDATE ON acl
FOR EACH ROW EXECUTE FUNCTION ecr_validate_acl_scope();

INSERT INTO schema_migration(version, checksum_sha256)
VALUES ('0008', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
