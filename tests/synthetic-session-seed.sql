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
  ('Synthetic ecommerce demo v1','demo-owner-a','owner',ARRAY['demo-shop-east','demo-shop-west']::text[],'Synthetic Owner A'),
  ('Synthetic ecommerce demo v1','demo-agent-west','operator',ARRAY['demo-shop-west']::text[],'Synthetic West Operator'),
  ('Synthetic ecommerce demo v1','demo-admin-a','admin',ARRAY['demo-shop-east','demo-shop-west']::text[],'Synthetic Admin A'),
  ('Synthetic ecommerce demo v1','demo-viewer-a','viewer',ARRAY['demo-shop-east']::text[],'Synthetic East Viewer'),
  ('Synthetic ecommerce demo v1','demo-no-shop','viewer',ARRAY[]::text[],'Synthetic No Shop'),
  ('Synthetic ecommerce demo v1','demo-revoked','operator',ARRAY['demo-shop-east']::text[],'Synthetic Revoked'),
  ('Synthetic ecommerce tenant b','demo-agent-central','operator',ARRAY['demo-shop-central']::text[],'Synthetic Tenant B Operator')
) AS values(tenant_name, external_id, role, shop_ids, display_name) ON values.tenant_name=t.name
ON CONFLICT (tenant_id, external_id) DO UPDATE SET role=EXCLUDED.role, shop_ids=EXCLUDED.shop_ids, display_name=EXCLUDED.display_name;

UPDATE app_user SET revoked_at = CASE WHEN external_id='demo-revoked' THEN now() ELSE NULL END,
    permission_revision = CASE WHEN external_id='demo-revoked' THEN 'auth-revoked-v1' ELSE 'auth-v1' END
WHERE external_id IN ('demo-owner-a','demo-agent-east','demo-agent-west','demo-admin-a','demo-viewer-a','demo-no-shop','demo-revoked','demo-agent-central');

-- Explicit trusted resource registry. Manifest principals/documents grant nothing.
WITH resources(tenant_name,shop_id,logical_key,classification) AS (VALUES
 ('Synthetic ecommerce demo v1','demo-shop-east','syn-products-a','external_allowed'),
 ('Synthetic ecommerce demo v1','demo-shop-west','syn-products-west','external_allowed'),
 ('Synthetic ecommerce demo v1','demo-shop-east','syn-policy-a','external_allowed'),
 ('Synthetic ecommerce demo v1','demo-shop-east','syn-restricted-a','external_allowed'),
 ('Synthetic ecommerce demo v1','demo-shop-east','syn-faq-a','external_allowed'),
 ('Synthetic ecommerce demo v1','demo-shop-east','syn-facts-a','internal_only'),
 ('Synthetic ecommerce demo v1','demo-shop-east','syn-acl-a','internal_only'),
 ('Synthetic ecommerce tenant b','demo-shop-central','syn-products-b','external_allowed'),
 ('Synthetic ecommerce tenant b','demo-shop-central','syn-policy-revoked','unclassified'))
INSERT INTO source(tenant_id,shop_id,type,uri)
SELECT t.id,r.shop_id,'synthetic-acl','synthetic://' || r.logical_key
FROM resources r JOIN tenant t ON t.name=r.tenant_name
WHERE NOT EXISTS(SELECT 1 FROM source s WHERE s.tenant_id=t.id AND s.uri='synthetic://' || r.logical_key);
INSERT INTO document(tenant_id,source_id,logical_key,title,doc_type,meta_json)
SELECT s.tenant_id,s.id,substring(s.uri from length('synthetic://')+1),'Synthetic resource','synthetic',
 jsonb_build_object('shop_id',s.shop_id,'disclosure_class',CASE
 WHEN s.uri IN ('synthetic://syn-facts-a','synthetic://syn-acl-a') THEN 'internal_only'
 WHEN s.uri='synthetic://syn-policy-revoked' THEN 'unclassified' ELSE 'external_allowed' END)
FROM source s JOIN tenant t ON t.id=s.tenant_id
WHERE t.name IN ('Synthetic ecommerce demo v1','Synthetic ecommerce tenant b')
 AND s.type='synthetic-acl'
ON CONFLICT(tenant_id,logical_key) DO NOTHING;

-- Database-backed ACL fixtures make the Go authorization boundary observable.
-- The local manifest remains the source of synthetic evidence; these rows only
-- decide whether a matching logical document may be returned to a principal.
INSERT INTO source (tenant_id, shop_id, type, uri)
SELECT t.id, 'demo-shop-east', 'synthetic-acl', 'synthetic://syn-policy-a'
FROM tenant t
WHERE t.name='Synthetic ecommerce demo v1'
  AND NOT EXISTS (
      SELECT 1 FROM source s
      WHERE s.tenant_id=t.id AND s.uri='synthetic://syn-policy-a'
  );
INSERT INTO source (tenant_id, shop_id, type, uri)
SELECT t.id, 'demo-shop-east', 'synthetic-acl', 'synthetic://syn-restricted-a'
FROM tenant t
WHERE t.name='Synthetic ecommerce demo v1'
  AND NOT EXISTS (
      SELECT 1 FROM source s
      WHERE s.tenant_id=t.id AND s.uri='synthetic://syn-restricted-a'
  );

INSERT INTO document (tenant_id, source_id, logical_key, title, doc_type, meta_json)
SELECT t.id, s.id, 'syn-policy-a', 'Synthetic policy ACL allow', 'policy',
       '{"shop_id":"demo-shop-east","disclosure_class":"external_allowed"}'::jsonb
FROM tenant t JOIN source s ON s.tenant_id=t.id AND s.uri='synthetic://syn-policy-a'
WHERE t.name='Synthetic ecommerce demo v1'
ON CONFLICT (tenant_id, logical_key) DO UPDATE
SET source_id=EXCLUDED.source_id, status='active', meta_json=EXCLUDED.meta_json;
INSERT INTO document (tenant_id, source_id, logical_key, title, doc_type, meta_json)
SELECT t.id, s.id, 'syn-restricted-a', 'Synthetic restricted ACL fixture', 'policy',
       '{"shop_id":"demo-shop-east","disclosure_class":"external_allowed"}'::jsonb
FROM tenant t JOIN source s ON s.tenant_id=t.id AND s.uri='synthetic://syn-restricted-a'
WHERE t.name='Synthetic ecommerce demo v1'
ON CONFLICT (tenant_id, logical_key) DO UPDATE
SET source_id=EXCLUDED.source_id, status='active', meta_json=EXCLUDED.meta_json;

DELETE FROM acl
WHERE tenant_id=(SELECT id FROM tenant WHERE name='Synthetic ecommerce demo v1')
  AND resource_type='document'
  AND resource_id IN (
      SELECT id FROM document
      WHERE tenant_id=(SELECT id FROM tenant WHERE name='Synthetic ecommerce demo v1')
        AND logical_key IN ('syn-policy-a','syn-restricted-a')
  );
INSERT INTO acl (tenant_id, resource_type, resource_id, subject_type, subject_id, permission)
SELECT t.id, 'document', d.id, 'role', roles.role, 'read'
FROM tenant t JOIN document d ON d.tenant_id=t.id AND d.logical_key='syn-policy-a' CROSS JOIN (VALUES ('operator'),('admin'),('owner'),('viewer')) roles(role)
WHERE t.name='Synthetic ecommerce demo v1';
INSERT INTO acl (tenant_id, resource_type, resource_id, subject_type, subject_id, permission)
SELECT t.id, 'document', d.id, 'role', roles.role, 'read'
FROM tenant t JOIN document d ON d.tenant_id=t.id AND d.logical_key='syn-restricted-a' CROSS JOIN (VALUES ('admin'),('owner')) roles(role)
WHERE t.name='Synthetic ecommerce demo v1';
COMMIT;
