BEGIN;
SELECT pg_advisory_xact_lock(742018);

-- The database keeps the compact synthetic permission keys used by the first
-- management API. This catalog is the versioned boundary to the role names in
-- the management design; unsupported roles remain explicitly disabled.
CREATE TABLE admin_role_catalog (
    role_key TEXT PRIMARY KEY,
    storage_role TEXT,
    display_name TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    requestable BOOLEAN NOT NULL,
    CHECK (role_key IN (
        'platform_observer','platform_access_admin','platform_role_approver',
        'support_permission_reviewer','merchant_admin','merchant_member'
    )),
    CHECK (storage_role IS NULL OR storage_role IN
        ('platform_observer','access_admin','merchant_admin','knowledge_reviewer')),
    CHECK (enabled OR NOT requestable)
);

INSERT INTO admin_role_catalog(role_key,storage_role,display_name,catalog_version,enabled,requestable)
VALUES
    ('platform_observer','platform_observer','平台观察员','admin-role-catalog-v1',true,false),
    ('platform_access_admin','access_admin','平台权限管理员','admin-role-catalog-v1',true,false),
    ('platform_role_approver',NULL,'平台角色审批人','admin-role-catalog-v1',false,false),
    ('support_permission_reviewer','knowledge_reviewer','客服权限审核员','admin-role-catalog-v1',true,false),
    ('merchant_admin','merchant_admin','商家管理员','admin-role-catalog-v1',true,true),
    ('merchant_member',NULL,'商家成员','admin-role-catalog-v1',false,false)
ON CONFLICT(role_key) DO UPDATE SET storage_role=EXCLUDED.storage_role,
    display_name=EXCLUDED.display_name,catalog_version=EXCLUDED.catalog_version,
    enabled=EXCLUDED.enabled,requestable=EXCLUDED.requestable;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_app') THEN
        GRANT SELECT ON admin_role_catalog TO rag_app;
    END IF;
END $$;

INSERT INTO schema_migration(version,checksum_sha256)
VALUES ('0014', :'migration_checksum') ON CONFLICT(version) DO NOTHING;
COMMIT;
