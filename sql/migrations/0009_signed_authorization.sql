-- Permission changes invalidate persisted executions and signed service scopes.
BEGIN;
SELECT pg_advisory_xact_lock(742018);

ALTER TABLE conversation_execution ADD COLUMN permission_revision TEXT;

CREATE OR REPLACE FUNCTION ecr_bump_user_permission_revision()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.role IS DISTINCT FROM OLD.role
       OR NEW.shop_ids IS DISTINCT FROM OLD.shop_ids
       OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
        NEW.permission_revision := ecr_new_permission_revision(NEW.tenant_id, 'app_user_changed');
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION ecr_invalidate_tenant_scope(p_tenant BIGINT, p_reason TEXT)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM tenant WHERE id=p_tenant) THEN
        UPDATE app_user SET permission_revision=ecr_new_permission_revision(p_tenant,p_reason)
        WHERE tenant_id=p_tenant;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ecr_bump_acl_permission_revision()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        PERFORM ecr_invalidate_tenant_scope(OLD.tenant_id,'acl_changed');
    END IF;
    IF TG_OP <> 'DELETE' AND (TG_OP='INSERT' OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id) THEN
        PERFORM ecr_invalidate_tenant_scope(NEW.tenant_id,'acl_changed');
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION ecr_bump_resource_permission_revision()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE old_tenant BIGINT; new_tenant BIGINT;
BEGIN
    IF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    IF TG_OP <> 'INSERT' THEN
        IF TG_TABLE_NAME='tenant' THEN old_tenant:=OLD.id; ELSE old_tenant:=OLD.tenant_id; END IF;
        PERFORM ecr_invalidate_tenant_scope(old_tenant,TG_TABLE_NAME || '_changed');
    END IF;
    IF TG_OP <> 'DELETE' THEN
        IF TG_TABLE_NAME='tenant' THEN new_tenant:=NEW.id; ELSE new_tenant:=NEW.tenant_id; END IF;
        IF new_tenant IS DISTINCT FROM old_tenant THEN
            PERFORM ecr_invalidate_tenant_scope(new_tenant,TG_TABLE_NAME || '_changed');
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_tenant_scope_revision AFTER UPDATE OF status ON tenant
FOR EACH ROW EXECUTE FUNCTION ecr_bump_resource_permission_revision();
CREATE TRIGGER trg_source_scope_revision AFTER INSERT OR UPDATE OR DELETE ON source
FOR EACH ROW EXECUTE FUNCTION ecr_bump_resource_permission_revision();
CREATE TRIGGER trg_document_scope_revision AFTER INSERT OR UPDATE OR DELETE ON document
FOR EACH ROW EXECUTE FUNCTION ecr_bump_resource_permission_revision();

REVOKE ALL ON FUNCTION ecr_invalidate_tenant_scope(BIGINT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION ecr_bump_resource_permission_revision() FROM PUBLIC;
REVOKE ALL ON FUNCTION ecr_bump_acl_permission_revision() FROM PUBLIC;
REVOKE ALL ON FUNCTION ecr_bump_user_permission_revision() FROM PUBLIC;

INSERT INTO schema_migration(version,checksum_sha256)
VALUES ('0009', :'migration_checksum');
COMMIT;
