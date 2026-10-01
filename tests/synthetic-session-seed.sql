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
COMMIT;
