-- Explicit development-only management grants for the synthetic demo identity.
-- Run only in the isolated m1_test database, using the migration/admin account.
BEGIN;
DO $$ BEGIN
    IF current_database() NOT LIKE '%m1_test%' THEN
        RAISE EXCEPTION 'synthetic admin seed requires an isolated m1_test database';
    END IF;
END $$;

INSERT INTO admin_role_grant(tenant_id,user_id,role,scope_kind,shop_ids,granted_by)
SELECT t.id,target.id,role.role,'shops',ARRAY['demo-shop-east','demo-shop-west']::text[],grantor.id
FROM tenant t
JOIN app_user target ON target.tenant_id=t.id AND target.external_id='demo-admin-a'
JOIN app_user grantor ON grantor.tenant_id=t.id AND grantor.external_id='demo-owner-a'
CROSS JOIN (VALUES ('access_admin'),('merchant_admin'),('knowledge_reviewer')) AS role(role)
WHERE t.name='Synthetic ecommerce demo v1'
ON CONFLICT(tenant_id,user_id,role) DO UPDATE SET scope_kind=EXCLUDED.scope_kind,
    shop_ids=EXCLUDED.shop_ids,status='active',granted_by=EXCLUDED.granted_by,
    granted_at=now(),revoked_by=NULL,revoked_at=NULL;

COMMIT;
