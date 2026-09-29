\set ON_ERROR_STOP on
BEGIN;
DO $$
DECLARE
    v_tenant_id BIGINT;
    v_user_id BIGINT;
    v_conversation_id BIGINT;
    v_turn_id BIGINT;
    v_execution_id BIGINT;
    v_source_id BIGINT;
    v_job_id BIGINT;
    request_id TEXT := 'm1-schema-smoke-' || txid_current()::text;
    rows_changed INT;
BEGIN
    SELECT t.id, u.id INTO v_tenant_id, v_user_id
    FROM tenant t JOIN app_user u ON u.tenant_id=t.id
    WHERE t.name='M1 isolated prototype' AND u.external_id='m1-user'
    ORDER BY t.id DESC LIMIT 1;
    IF v_tenant_id IS NULL OR v_user_id IS NULL THEN RAISE EXCEPTION 'M1 mock identity seed is missing'; END IF;

    INSERT INTO conversation(tenant_id,user_id,shop_id,title)
    VALUES(v_tenant_id,v_user_id,'shop-demo','schema smoke') RETURNING id INTO v_conversation_id;
    INSERT INTO turn(tenant_id,conversation_id,turn_no,request_id,user_query,status)
    VALUES(v_tenant_id,v_conversation_id,1,request_id,'fixture query','EXECUTING') RETURNING id INTO v_turn_id;
    INSERT INTO conversation_execution(tenant_id,turn_id,execution_no,request_id,status,is_mock,deadline_at)
    VALUES(v_tenant_id,v_turn_id,1,request_id,'EXECUTING',true,now()+interval '60 seconds')
    RETURNING id INTO v_execution_id;
    INSERT INTO turn_idempotency(tenant_id,conversation_id,idempotency_key,payload_hash,turn_id)
    VALUES(v_tenant_id,v_conversation_id,'smoke-key','payload-a',v_turn_id);
    IF (SELECT payload_hash FROM turn_idempotency WHERE tenant_id=v_tenant_id AND conversation_id=v_conversation_id
        AND idempotency_key='smoke-key') <> 'payload-a' THEN
        RAISE EXCEPTION 'idempotency payload hash round-trip failed';
    END IF;

    UPDATE turn SET status='ASKING' WHERE tenant_id=v_tenant_id AND id=v_turn_id;
    INSERT INTO turn(tenant_id,conversation_id,turn_no,request_id,user_query,status,parent_turn_id)
    VALUES(v_tenant_id,v_conversation_id,2,request_id || '-child','补充后的问题','EXECUTING',v_turn_id)
    RETURNING id INTO v_turn_id;
    IF (SELECT parent_turn_id FROM turn WHERE tenant_id=v_tenant_id AND id=v_turn_id) IS NULL THEN
        RAISE EXCEPTION 'clarification parent linkage failed';
    END IF;

    INSERT INTO evidence_snapshot(tenant_id,execution_id,evidence_id,document_id,version_id,source_ref,
        content,disclosure_class,customer_eligible,citation_index)
    VALUES(v_tenant_id,v_execution_id,'smoke-evidence','smoke-doc','smoke-version','fixture://smoke',
        'fictional content','external_allowed',true,1);
    INSERT INTO citation(tenant_id,execution_id,evidence_id,citation_index)
    VALUES(v_tenant_id,v_execution_id,'smoke-evidence',1);
    INSERT INTO customer_reply(tenant_id,execution_id,text_plain,is_mock,can_copy,blocked_reason)
    VALUES(v_tenant_id,v_execution_id,'mock text',true,false,'mock');

    BEGIN
        UPDATE customer_reply cr SET can_copy=true WHERE cr.tenant_id=v_tenant_id AND cr.execution_id=v_execution_id;
        RAISE EXCEPTION 'mock customer reply unexpectedly became copyable';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    INSERT INTO sse_event(tenant_id,execution_id,sequence,event_type,data_json)
    VALUES(v_tenant_id,v_execution_id,1,'status','{"status":"EXECUTING"}');
    IF NOT EXISTS (SELECT 1 FROM sse_event se WHERE se.tenant_id=v_tenant_id AND se.execution_id=v_execution_id AND se.sequence=1) THEN
        RAISE EXCEPTION 'SSE event persistence failed';
    END IF;

    INSERT INTO source(tenant_id,shop_id,type,uri) VALUES(v_tenant_id,'shop-demo','fixture','fixture://smoke') RETURNING id INTO v_source_id;
    INSERT INTO ingest_job(tenant_id,source_id,idempotency_key,stage,status)
    VALUES(v_tenant_id,v_source_id,'m1-smoke-' || txid_current()::text,'fixture','running') RETURNING id INTO v_job_id;
    INSERT INTO ingest_job_item(tenant_id,job_id,data_id,payload_hash,status,owner_token,fencing_epoch,lease_expires_at)
    VALUES(v_tenant_id,v_job_id,'fixture-a','payload-a','running','worker-old',1,now()+interval '1 minute');
    UPDATE ingest_job_item SET lease_expires_at=now()-interval '1 second'
    WHERE tenant_id=v_tenant_id AND job_id=v_job_id AND data_id='fixture-a';
    UPDATE ingest_job_item SET owner_token='worker-new',fencing_epoch=2,lease_expires_at=now()+interval '1 minute'
    WHERE tenant_id=v_tenant_id AND job_id=v_job_id AND data_id='fixture-a';
    UPDATE ingest_job_item SET status='done'
    WHERE tenant_id=v_tenant_id AND job_id=v_job_id AND data_id='fixture-a'
      AND owner_token='worker-old' AND fencing_epoch=1;
    GET DIAGNOSTICS rows_changed = ROW_COUNT;
    IF rows_changed <> 0 THEN RAISE EXCEPTION 'stale fencing owner was allowed to publish'; END IF;
    BEGIN
        INSERT INTO ingest_publication(tenant_id,item_id,fencing_epoch,document_version_id)
        SELECT v_tenant_id,id,1,0 FROM ingest_job_item
        WHERE tenant_id=v_tenant_id AND job_id=v_job_id AND data_id='fixture-a';
        RAISE EXCEPTION 'stale fencing owner unexpectedly published';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;
END $$;
ROLLBACK;
