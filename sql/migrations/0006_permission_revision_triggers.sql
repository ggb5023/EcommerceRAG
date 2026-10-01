-- Keep the synthetic permission revision current when authorization data changes.
-- Development fixture only; this does not implement OIDC or production identity.
BEGIN;
SELECT pg_advisory_xact_lock(742018);

ALTER TABLE evidence_snapshot ADD COLUMN IF NOT EXISTS shop_id TEXT;

CREATE OR REPLACE FUNCTION ecr_new_permission_revision(p_tenant_id BIGINT, p_reason TEXT)
RETURNS TEXT
LANGUAGE plpgsql
AS $$
DECLARE
    value TEXT;
BEGIN
    value := 'auth-' || substr(md5(clock_timestamp()::text || random()::text), 1, 24);
    INSERT INTO permission_revision(tenant_id, revision, reason)
    VALUES (p_tenant_id, value, p_reason)
    ON CONFLICT (tenant_id, revision) DO NOTHING;
    RETURN value;
END;
$$;

CREATE OR REPLACE FUNCTION ecr_bump_user_permission_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.role IS DISTINCT FROM OLD.role
       OR NEW.shop_ids IS DISTINCT FROM OLD.shop_ids
       OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
        IF NEW.permission_revision IS NOT DISTINCT FROM OLD.permission_revision THEN
            NEW.permission_revision := ecr_new_permission_revision(NEW.tenant_id, 'app_user_changed');
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_app_user_permission_revision ON app_user;
CREATE TRIGGER trg_app_user_permission_revision
BEFORE UPDATE OF role, shop_ids, revoked_at ON app_user
FOR EACH ROW EXECUTE FUNCTION ecr_bump_user_permission_revision();

CREATE OR REPLACE FUNCTION ecr_bump_acl_permission_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    tenant BIGINT;
    revision TEXT;
BEGIN
    tenant := COALESCE(NEW.tenant_id, OLD.tenant_id);
    revision := ecr_new_permission_revision(tenant, 'acl_changed');
    UPDATE app_user
       SET permission_revision = revision
     WHERE tenant_id = tenant;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_acl_permission_revision ON acl;
CREATE TRIGGER trg_acl_permission_revision
AFTER INSERT OR UPDATE OR DELETE ON acl
FOR EACH ROW EXECUTE FUNCTION ecr_bump_acl_permission_revision();

CREATE OR REPLACE FUNCTION ecr_bump_shop_permission_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    revision TEXT;
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        revision := ecr_new_permission_revision(NEW.tenant_id, 'shop_status_changed');
        UPDATE app_user
           SET permission_revision = revision
         WHERE tenant_id = NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_shop_permission_revision ON shop;
CREATE TRIGGER trg_shop_permission_revision
AFTER UPDATE OF status ON shop
FOR EACH ROW EXECUTE FUNCTION ecr_bump_shop_permission_revision();

INSERT INTO schema_migration(version, checksum_sha256)
VALUES ('0006', :'migration_checksum') ON CONFLICT (version) DO NOTHING;

COMMIT;
