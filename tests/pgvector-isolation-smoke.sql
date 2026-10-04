\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
    tenant_a BIGINT;
    tenant_b BIGINT;
    reviewer_a BIGINT;
    source_a BIGINT;
    source_b BIGINT;
    document_a BIGINT;
    document_b BIGINT;
    old_version BIGINT;
    active_version BIGINT;
    active_b BIGINT;
    result_tenant BIGINT;
    result_version BIGINT;
    result_chunk INT;
    result_count INT;
    query_vector halfvec(1024) := array_prepend(2::real, array_fill(1::real, ARRAY[1023]))::halfvec(1024);
BEGIN
    SELECT id INTO tenant_a FROM tenant ORDER BY id LIMIT 1;
    SELECT id INTO tenant_b FROM tenant ORDER BY id OFFSET 1 LIMIT 1;
    SELECT id INTO reviewer_a FROM app_user WHERE tenant_id = tenant_a ORDER BY id LIMIT 1;
    IF tenant_a IS NULL OR tenant_b IS NULL OR reviewer_a IS NULL THEN
        RAISE EXCEPTION 'isolated pgvector smoke requires two existing tenants and one reviewer user';
    END IF;
    INSERT INTO source(tenant_id, shop_id, type, uri)
    VALUES (tenant_a, 'shop-a', 'fixture', 'fixture://pgvector-a') RETURNING id INTO source_a;
    INSERT INTO source(tenant_id, shop_id, type, uri)
    VALUES (tenant_b, 'shop-a', 'fixture', 'fixture://pgvector-b') RETURNING id INTO source_b;
    INSERT INTO document(tenant_id, source_id, logical_key, title, doc_type)
    VALUES (tenant_a, source_a, 'pgvector-doc-a-' || txid_current(), 'fixture A', 'fixture')
    RETURNING id INTO document_a;
    INSERT INTO document(tenant_id, source_id, logical_key, title, doc_type)
    VALUES (tenant_b, source_b, 'pgvector-doc-b-' || txid_current(), 'fixture B', 'fixture')
    RETURNING id INTO document_b;

    INSERT INTO document_version(
        tenant_id, document_id, source_hash, parser_version, chunk_rule_version,
        embedding_model, pipeline_version, object_key, status, chunk_count,
        disclosure_class, external_allowed, review_status, reviewed_hash,
        reviewed_by, reviewed_at
    ) VALUES (
        tenant_a, document_a, repeat('1', 64), 'fixture-parser', 'structured-v2',
        'fake-1024', 'pgvector-smoke', 'fixture://old', 'superseded', 1,
        'external_allowed', true, 'approved', repeat('1', 64), reviewer_a, now()
    ) RETURNING id INTO old_version;
    INSERT INTO document_version(
        tenant_id, document_id, source_hash, parser_version, chunk_rule_version,
        embedding_model, pipeline_version, object_key, status, chunk_count,
        disclosure_class, external_allowed, review_status, reviewed_hash,
        reviewed_by, reviewed_at
    ) VALUES (
        tenant_a, document_a, repeat('2', 64), 'fixture-parser', 'structured-v2',
        'fake-1024', 'pgvector-smoke', 'fixture://active', 'ready', 3,
        'external_allowed', true, 'approved', repeat('2', 64), reviewer_a, now()
    ) RETURNING id INTO active_version;
    INSERT INTO document_version(
        tenant_id, document_id, source_hash, parser_version, chunk_rule_version,
        embedding_model, pipeline_version, object_key, status, chunk_count,
        disclosure_class, external_allowed, review_status, reviewed_hash,
        reviewed_by, reviewed_at
    ) VALUES (
        tenant_b, document_b, repeat('3', 64), 'fixture-parser', 'structured-v2',
        'fake-1024', 'pgvector-smoke', 'fixture://tenant-b', 'ready', 1,
        'external_allowed', true, 'approved', repeat('3', 64), reviewer_a, now()
    ) RETURNING id INTO active_b;
    UPDATE document SET active_version_id = active_version WHERE id = document_a;
    UPDATE document SET active_version_id = active_b WHERE id = document_b;

    INSERT INTO chunk(
        version_id, doc_id, tenant_id, shop_id, chunk_index, section_seq,
        section_chunk_index, content, embed_text, token_count, content_type,
        split_reason, embedding, embedding_model, embedding_dim, tokenizer_id,
        effective_from, effective_to, chunk_hash
    ) VALUES
        (active_version, document_a, tenant_a, 'shop-a', 0, 1, 0,
         'active nearest', 'active nearest', 2, 'text', 'section', query_vector,
         'fake-1024', 1024, 'fixture', DATE '2026-01-01', NULL, repeat('a', 64)),
        (active_version, document_a, tenant_a, 'shop-b', 1, 1, 1,
         'other shop', 'other shop', 2, 'text', 'section',
         array_fill(1::real, ARRAY[1024])::halfvec(1024),
         'fake-1024', 1024, 'fixture', DATE '2026-01-01', NULL, repeat('b', 64)),
        (active_version, document_a, tenant_a, 'shop-a', 2, 1, 2,
         'expired', 'expired', 1, 'text', 'section', query_vector,
         'fake-1024', 1024, 'fixture', DATE '2025-01-01', DATE '2025-12-31', repeat('c', 64)),
        (old_version, document_a, tenant_a, 'shop-a', 0, 1, 0,
         'old version', 'old version', 2, 'text', 'section', query_vector,
         'fake-1024', 1024, 'fixture', DATE '2026-01-01', NULL, repeat('d', 64)),
        (active_b, document_b, tenant_b, 'shop-a', 0, 1, 0,
         'other tenant', 'other tenant', 2, 'text', 'section', query_vector,
         'fake-1024', 1024, 'fixture', DATE '2026-01-01', NULL, repeat('e', 64));

    SELECT c.tenant_id, c.version_id, c.chunk_index INTO result_tenant, result_version, result_chunk
    FROM chunk c JOIN document d ON d.tenant_id = c.tenant_id AND d.id = c.doc_id
    WHERE c.tenant_id = tenant_a AND c.shop_id = 'shop-a'
      AND c.version_id = d.active_version_id
      AND (c.effective_from IS NULL OR c.effective_from <= DATE '2026-10-04')
      AND (c.effective_to IS NULL OR c.effective_to >= DATE '2026-10-04')
      AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> query_vector, c.chunk_index
    LIMIT 1;
    IF result_tenant <> tenant_a OR result_version <> active_version OR result_chunk <> 0 THEN
        RAISE EXCEPTION 'authorized top-k returned wrong tenant, version or chunk';
    END IF;

    SELECT count(*) INTO result_count
    FROM chunk c JOIN document d ON d.tenant_id = c.tenant_id AND d.id = c.doc_id
    WHERE c.tenant_id = tenant_a AND c.shop_id = 'shop-a'
      AND c.version_id = d.active_version_id
      AND (c.effective_from IS NULL OR c.effective_from <= DATE '2026-10-04')
      AND (c.effective_to IS NULL OR c.effective_to >= DATE '2026-10-04');
    IF result_count <> 1 THEN
        RAISE EXCEPTION 'tenant/shop/version/date filters returned % rows', result_count;
    END IF;

    BEGIN
        INSERT INTO chunk(
            version_id, doc_id, tenant_id, shop_id, chunk_index, section_seq,
            section_chunk_index, content, embed_text, token_count, content_type,
            split_reason, embedding_model, embedding_dim, tokenizer_id, chunk_hash
        ) VALUES (active_version, document_a, tenant_a, 'shop-a', 0, 1, 0,
            'duplicate', 'duplicate', 1, 'text', 'section', 'fake-1024', 1024,
            'fixture', repeat('f', 64));
        RAISE EXCEPTION 'duplicate chunk key was accepted';
    EXCEPTION WHEN unique_violation THEN
        NULL;
    END;
END $$;

ROLLBACK;
SELECT 'PASS pgvector top-k tenant/shop/version/date filters and idempotency' AS check;
