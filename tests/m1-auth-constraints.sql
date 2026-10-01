-- Transactional database checks for migration 0008. Nothing is retained.
\set ON_ERROR_STOP on
BEGIN;
DO $$
DECLARE
    tenant_a BIGINT;
    tenant_b BIGINT;
    user_a BIGINT;
    user_b BIGINT;
    source_a BIGINT;
    document_a BIGINT;
    document_b BIGINT;
    rev_a TEXT;
    rev_b TEXT;
    raised BOOLEAN;
BEGIN
    INSERT INTO tenant(name, status) VALUES ('__m1_constraint_tenant_a__', 'active') RETURNING id INTO tenant_a;
    INSERT INTO tenant(name, status) VALUES ('__m1_constraint_tenant_b__', 'active') RETURNING id INTO tenant_b;
    INSERT INTO app_user(tenant_id, external_id, role, shop_ids)
    VALUES (tenant_a, '__m1_constraint_user_a__', 'operator', ARRAY['shop-a']) RETURNING id INTO user_a;
    INSERT INTO app_user(tenant_id, external_id, role, shop_ids)
    VALUES (tenant_b, '__m1_constraint_user_b__', 'operator', ARRAY['shop-b']) RETURNING id INTO user_b;
    INSERT INTO source(tenant_id, shop_id, type, uri)
    VALUES (tenant_a, 'shop-a', 'constraint-test', 'synthetic://constraint-a') RETURNING id INTO source_a;
    INSERT INTO document(tenant_id, source_id, logical_key, title, doc_type)
    VALUES (tenant_a, source_a, '__m1_constraint_document_a__', 'constraint a', 'test') RETURNING id INTO document_a;
    INSERT INTO document(tenant_id, source_id, logical_key, title, doc_type)
    VALUES (tenant_b, NULL, '__m1_constraint_document_b__', 'constraint b', 'test') RETURNING id INTO document_b;

    raised := false;
    BEGIN
        INSERT INTO app_user(tenant_id, external_id, role) VALUES (tenant_a, '__m1_bad_role__', 'superuser');
    EXCEPTION WHEN check_violation THEN
        raised := true;
    END;
    IF NOT raised THEN RAISE EXCEPTION 'role CHECK did not reject invalid role'; END IF;

    raised := false;
    BEGIN
        INSERT INTO acl(tenant_id, resource_type, resource_id, subject_type, subject_id, permission)
        VALUES (tenant_a, 'document', document_b, 'role', 'operator', 'read');
    EXCEPTION WHEN foreign_key_violation THEN
        raised := true;
    END;
    IF NOT raised THEN RAISE EXCEPTION 'ACL resource tenant mismatch was accepted'; END IF;

    raised := false;
    BEGIN
        INSERT INTO acl(tenant_id, resource_type, resource_id, subject_type, subject_id, permission)
        VALUES (tenant_a, 'document', document_a, 'user', '__m1_constraint_user_b__', 'read');
    EXCEPTION WHEN foreign_key_violation THEN
        raised := true;
    END;
    IF NOT raised THEN RAISE EXCEPTION 'ACL user tenant mismatch was accepted'; END IF;

    raised := false;
    BEGIN
        INSERT INTO acl(tenant_id, resource_type, resource_id, subject_type, subject_id, permission)
        VALUES (tenant_a, 'document', document_a, 'role', 'superuser', 'read');
    EXCEPTION WHEN check_violation THEN
        raised := true;
    END;
    IF NOT raised THEN RAISE EXCEPTION 'ACL role CHECK did not reject invalid role'; END IF;

    INSERT INTO acl(tenant_id, resource_type, resource_id, subject_type, subject_id, permission)
    VALUES (tenant_a, 'document', document_a, 'user', '__m1_constraint_user_a__', 'read');

    SELECT permission_revision INTO rev_a FROM app_user WHERE id=user_a;
    UPDATE app_user SET role='viewer',permission_revision=rev_a WHERE id=user_a;
    IF (SELECT permission_revision FROM app_user WHERE id=user_a)=rev_a THEN
        RAISE EXCEPTION 'supplied revision bypassed role change invalidation';
    END IF;

    INSERT INTO acl(tenant_id,resource_type,resource_id,subject_type,subject_id,permission)
    VALUES(tenant_a,'tenant',tenant_a,'role','admin','read');
    SELECT permission_revision INTO rev_a FROM app_user WHERE id=user_a;
    SELECT permission_revision INTO rev_b FROM app_user WHERE id=user_b;
    UPDATE acl SET tenant_id=tenant_b,resource_id=tenant_b
    WHERE tenant_id=tenant_a AND resource_type='tenant' AND subject_id='admin';
    IF (SELECT permission_revision FROM app_user WHERE id=user_a)=rev_a
       OR (SELECT permission_revision FROM app_user WHERE id=user_b)=rev_b THEN
        RAISE EXCEPTION 'moved ACL did not invalidate both tenant scopes';
    END IF;
END;
$$;
ROLLBACK;
SELECT 'PASS synthetic authorization constraints' AS check;
