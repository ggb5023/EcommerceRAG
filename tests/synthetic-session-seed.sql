-- Development-only seed for the opt-in synthetic HTTP smoke.
-- This is not a migration and must only run in an isolated test database.
BEGIN;
INSERT INTO tenant (name, status)
SELECT 'Synthetic ecommerce demo v1', 'active'
WHERE NOT EXISTS (SELECT 1 FROM tenant WHERE name = 'Synthetic ecommerce demo v1');

INSERT INTO app_user (tenant_id, external_id, role, shop_ids)
SELECT id, 'demo-agent-east', 'operator', ARRAY['demo-shop-east']
FROM tenant WHERE name = 'Synthetic ecommerce demo v1'
ON CONFLICT (tenant_id, external_id) DO UPDATE
SET role = EXCLUDED.role, shop_ids = EXCLUDED.shop_ids;

INSERT INTO shop (tenant_id, id, name, status)
SELECT id, 'demo-shop-east', 'Synthetic East Shop', 'active'
FROM tenant WHERE name = 'Synthetic ecommerce demo v1'
ON CONFLICT (tenant_id, id) DO UPDATE SET name = EXCLUDED.name, status = 'active';

INSERT INTO shop (tenant_id, id, name, status)
SELECT id, 'demo-shop-west', 'Synthetic West Shop', 'active'
FROM tenant WHERE name = 'Synthetic ecommerce demo v1'
ON CONFLICT (tenant_id, id) DO UPDATE SET name = EXCLUDED.name, status = 'active';

INSERT INTO tenant (name, status)
SELECT 'Synthetic ecommerce tenant b', 'active'
WHERE NOT EXISTS (SELECT 1 FROM tenant WHERE name = 'Synthetic ecommerce tenant b');
INSERT INTO shop (tenant_id, id, name, status)
SELECT id, 'demo-shop-central', 'Synthetic Central Shop', 'active'
FROM tenant WHERE name = 'Synthetic ecommerce tenant b'
ON CONFLICT (tenant_id, id) DO UPDATE SET name = EXCLUDED.name, status = 'active';

INSERT INTO app_user (tenant_id, external_id, role, shop_ids, display_name)
SELECT id, values.external_id, values.role, values.shop_ids, values.display_name
FROM tenant t
JOIN (VALUES
  ('Synthetic ecommerce demo v1','demo-agent-west','operator',ARRAY['demo-shop-west']::text[],'Synthetic West Operator'),
  ('Synthetic ecommerce demo v1','demo-admin-a','admin',ARRAY['demo-shop-east','demo-shop-west']::text[],'Synthetic Admin A'),
  ('Synthetic ecommerce demo v1','demo-no-shop','viewer',ARRAY[]::text[],'Synthetic No Shop'),
  ('Synthetic ecommerce demo v1','demo-revoked','operator',ARRAY['demo-shop-east']::text[],'Synthetic Revoked'),
  ('Synthetic ecommerce tenant b','demo-agent-central','operator',ARRAY['demo-shop-central']::text[],'Synthetic Tenant B Operator')
) AS values(tenant_name, external_id, role, shop_ids, display_name) ON values.tenant_name=t.name
ON CONFLICT (tenant_id, external_id) DO UPDATE SET role=EXCLUDED.role, shop_ids=EXCLUDED.shop_ids, display_name=EXCLUDED.display_name;

UPDATE app_user SET revoked_at = CASE WHEN external_id='demo-revoked' THEN now() ELSE NULL END,
    permission_revision = CASE WHEN external_id='demo-revoked' THEN 'auth-revoked-v1' ELSE 'auth-v1' END
WHERE external_id IN ('demo-agent-east','demo-agent-west','demo-admin-a','demo-no-shop','demo-revoked','demo-agent-central');
COMMIT;
