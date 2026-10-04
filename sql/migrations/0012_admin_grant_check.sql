BEGIN;
SELECT pg_advisory_xact_lock(742018);

CREATE FUNCTION ecr_check_admin_grant(
    p_tenant_id BIGINT,p_user_id BIGINT,p_role TEXT,p_shop_id TEXT
) RETURNS BOOLEAN LANGUAGE SQL SECURITY DEFINER SET search_path=public,pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM admin_role_grant gr
        JOIN app_user u ON u.tenant_id=gr.tenant_id AND u.id=gr.user_id
        JOIN tenant t ON t.id=u.tenant_id
        WHERE gr.tenant_id=p_tenant_id AND gr.user_id=p_user_id AND gr.role=p_role AND gr.status='active'
          AND u.revoked_at IS NULL AND t.status='active'
          AND (u.shop_ids IS NULL OR p_shop_id=ANY(u.shop_ids))
          AND (gr.scope_kind='tenant' OR p_shop_id=ANY(gr.shop_ids))
        FOR SHARE OF gr,u,t
    );
$$;
REVOKE ALL ON FUNCTION ecr_check_admin_grant(BIGINT,BIGINT,TEXT,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ecr_check_admin_grant(BIGINT,BIGINT,TEXT,TEXT) TO rag_app;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_app') THEN
        RAISE EXCEPTION 'rag_app role is required for management authorization';
    END IF;
END $$;

INSERT INTO schema_migration(version,checksum_sha256)
VALUES ('0012', :'migration_checksum') ON CONFLICT(version) DO NOTHING;
COMMIT;
