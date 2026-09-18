\set ON_ERROR_STOP on
INSERT INTO tenant(id,name) VALUES (1,'schema-test-a'),(2,'schema-test-b');
INSERT INTO app_user(id,tenant_id,role) VALUES (1,1,'owner'),(2,2,'owner');
INSERT INTO source(id,tenant_id,type) VALUES (1,1,'upload');
INSERT INTO document(id,tenant_id,source_id,logical_key) VALUES (1,1,1,'fixture');
INSERT INTO document_version(id,tenant_id,document_id,source_hash,parser_version,chunk_rule_version,embedding_model,pipeline_version,object_key,status)
VALUES (1,1,1,'fixture','p1','c1','e1','all-v1','fixture.pdf','ready');
UPDATE document SET active_version_id=1 WHERE id=1;
INSERT INTO chunk(version_id,doc_id,tenant_id,chunk_index,section_seq,section_chunk_index,content,embed_text,token_count,content_type,split_reason,embedding,embedding_model,embedding_dim,tokenizer_id,chunk_hash)
VALUES (1,1,1,0,1,0,'fixture','fixture',1,'text','section',array_fill(1::real,ARRAY[1024])::halfvec(1024),'e1',1024,'fixture','fixture');
INSERT INTO ingest_job(tenant_id,source_id,idempotency_key,stage,status) VALUES (1,1,'key1','parse','pending');
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';
  IF n<>19 THEN RAISE EXCEPTION 'Expected 18 domain tables plus migration, got %', n; END IF;
  BEGIN
    INSERT INTO ingest_job(tenant_id,source_id,idempotency_key,stage,status) VALUES (1,1,'key1','parse','pending');
    RAISE EXCEPTION 'Duplicate job was accepted';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;
  BEGIN
    INSERT INTO conversation(tenant_id,user_id) VALUES (1,2);
    RAISE EXCEPTION 'Cross-tenant user was accepted';
  EXCEPTION WHEN foreign_key_violation THEN NULL;
  END;
  BEGIN
    INSERT INTO document_version(id,tenant_id,document_id,source_hash,parser_version,chunk_rule_version,embedding_model,pipeline_version,object_key,status)
    VALUES (2,2,1,'bad','p','c','e','p','bad','ready');
    RAISE EXCEPTION 'Cross-tenant document was accepted';
  EXCEPTION WHEN foreign_key_violation THEN NULL;
  END;
  IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_chunk_embedding' AND indexdef LIKE '%halfvec_cosine_ops%') THEN
    RAISE EXCEPTION 'Missing halfvec HNSW index';
  END IF;
  SELECT count(*) INTO n FROM chunk c JOIN document d ON c.doc_id=d.id AND c.tenant_id=d.tenant_id
    WHERE c.tenant_id=1 AND c.version_id=d.active_version_id;
  IF n<>1 THEN RAISE EXCEPTION 'Active-version query mismatch'; END IF;
  SELECT count(*) INTO n FROM chunk WHERE tenant_id=2;
  IF n<>0 THEN RAISE EXCEPTION 'Tenant filter mismatch'; END IF;
END $$;
SELECT id FROM chunk WHERE tenant_id=1 ORDER BY embedding <=> array_fill(1::real,ARRAY[1024])::halfvec(1024) LIMIT 1;
