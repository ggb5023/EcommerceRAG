\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
    tenant_key BIGINT;
    user_key BIGINT;
    source_key BIGINT;
    job_key BIGINT;
    item_key BIGINT;
    changed INT;
BEGIN
    SELECT t.id, u.id INTO tenant_key, user_key
    FROM tenant t JOIN app_user u ON u.tenant_id = t.id
    WHERE t.status = 'active'
    ORDER BY t.id, u.id
    LIMIT 1;
    IF tenant_key IS NULL OR user_key IS NULL THEN
        RAISE EXCEPTION 'worker smoke requires an existing active tenant and user';
    END IF;

    INSERT INTO source(tenant_id, shop_id, type, uri)
    VALUES (tenant_key, 'worker-smoke-shop', 'fixture', 'fixture://worker-smoke')
    RETURNING id INTO source_key;
    INSERT INTO ingest_job(
        tenant_id, source_id, idempotency_key, stage, status, submitted_by,
        manifest_sha256, dataset_sha256, next_attempt_at, max_attempts
    ) VALUES (
        tenant_key, source_key, 'worker-smoke-' || txid_current(), 'fixture', 'pending', user_key,
        repeat('1', 64), repeat('2', 64), now(), 3
    ) RETURNING id INTO job_key;
    INSERT INTO ingest_job_item(
        tenant_id, job_id, data_id, payload_hash, status, owner_token,
        fencing_epoch, lease_expires_at, next_attempt_at
    ) VALUES (
        tenant_key, job_key, 'worker-item', repeat('3', 64), 'running',
        'worker-old', 1, now() - interval '1 second', now()
    ) RETURNING id INTO item_key;

    UPDATE ingest_job
    SET status='running', owner_token='worker-old', fencing_epoch=1,
        lease_expires_at=now() - interval '1 second'
    WHERE tenant_id=tenant_key AND id=job_key;
    UPDATE ingest_job_item
    SET owner_token='worker-new', fencing_epoch=2,
        lease_expires_at=now() + interval '1 minute'
    WHERE tenant_id=tenant_key AND id=item_key AND status='running'
      AND lease_expires_at <= now() AND fencing_epoch=1;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 1 THEN RAISE EXCEPTION 'expired item lease was not taken over'; END IF;

    UPDATE ingest_job_item SET status='done'
    WHERE tenant_id=tenant_key AND id=item_key AND owner_token='worker-old' AND fencing_epoch=1;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 0 THEN RAISE EXCEPTION 'stale worker changed an item'; END IF;

    UPDATE ingest_job SET cancel_requested=true, status='running'
    WHERE tenant_id=tenant_key AND id=job_key AND owner_token='worker-new' AND fencing_epoch=2;
    UPDATE ingest_job_item SET cancel_requested=true
    WHERE tenant_id=tenant_key AND id=item_key AND owner_token='worker-new' AND fencing_epoch=2;
    UPDATE ingest_job_item SET status='done'
    WHERE tenant_id=tenant_key AND id=item_key AND owner_token='worker-new'
      AND fencing_epoch=2 AND NOT cancel_requested AND lease_expires_at > now();
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 0 THEN RAISE EXCEPTION 'cancelled worker changed an item'; END IF;

    UPDATE ingest_job SET status='failed', stage='parse', error_code='parser_unavailable'
    WHERE tenant_id=tenant_key AND id=job_key;
    UPDATE ingest_job SET status='pending', stage='retry_wait', retry_count=retry_count+1,
        cancel_requested=false, owner_token=NULL, fencing_epoch=fencing_epoch+1, lease_expires_at=NULL,
        next_attempt_at=now() WHERE tenant_id=tenant_key AND id=job_key;
    UPDATE ingest_job_item SET status='pending', stage='queued', cancel_requested=false,
        owner_token=NULL, fencing_epoch=fencing_epoch+1, lease_expires_at=NULL, next_attempt_at=now()
    WHERE tenant_id=tenant_key AND id=item_key;
    IF NOT EXISTS (SELECT 1 FROM ingest_job WHERE tenant_id=tenant_key AND id=job_key AND status='pending' AND retry_count=1)
       OR NOT EXISTS (SELECT 1 FROM ingest_job_item WHERE tenant_id=tenant_key AND id=item_key AND status='pending') THEN
        RAISE EXCEPTION 'retry transition did not reset job and item';
    END IF;
END $$;

ROLLBACK;
SELECT 'PASS ingest lease takeover, stale worker fencing, cancellation and retry transitions' AS check;
