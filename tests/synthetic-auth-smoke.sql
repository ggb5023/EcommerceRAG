-- Run after synthetic-session-seed.sql with a read-capable account.
\set ON_ERROR_STOP on
SELECT 'PASS tenant-a east operator' AS check
WHERE EXISTS (SELECT 1 FROM app_user u JOIN tenant t ON t.id=u.tenant_id
              WHERE t.name='Synthetic ecommerce demo v1' AND u.external_id='demo-agent-east'
                AND u.role='operator' AND u.shop_ids @> ARRAY['demo-shop-east'] AND u.revoked_at IS NULL);
SELECT 'PASS tenant-a west operator' AS check
WHERE EXISTS (SELECT 1 FROM app_user u JOIN tenant t ON t.id=u.tenant_id
              WHERE t.name='Synthetic ecommerce demo v1' AND u.external_id='demo-agent-west'
                AND u.shop_ids @> ARRAY['demo-shop-west'] AND u.revoked_at IS NULL);
SELECT 'PASS admin multi-shop' AS check
WHERE EXISTS (SELECT 1 FROM app_user u WHERE u.external_id='demo-admin-a'
              AND u.role='admin' AND u.shop_ids @> ARRAY['demo-shop-east','demo-shop-west']);
SELECT 'PASS empty shop denied' AS check
WHERE EXISTS (SELECT 1 FROM app_user u WHERE u.external_id='demo-no-shop' AND cardinality(u.shop_ids)=0);
SELECT 'PASS revoked user denied' AS check
WHERE EXISTS (SELECT 1 FROM app_user u WHERE u.external_id='demo-revoked' AND u.revoked_at IS NOT NULL);
SELECT 'PASS tenant-b isolated' AS check
WHERE EXISTS (SELECT 1 FROM app_user u JOIN tenant t ON t.id=u.tenant_id
              WHERE t.name='Synthetic ecommerce tenant b' AND u.external_id='demo-agent-central');
