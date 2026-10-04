BEGIN;

DO $$
DECLARE
    status_definition TEXT;
    function_signature TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO status_definition
      FROM pg_constraint
      WHERE conrelid='public.ingest_job'::regclass AND conname='ingest_job_status_check';
    IF status_definition IS NULL OR position('awaiting_review' IN status_definition)=0 THEN
        RAISE EXCEPTION 'ingest_job does not support awaiting_review';
    END IF;

    IF has_table_privilege(current_user,'public.admin_role_grant','UPDATE')
       OR has_table_privilege(current_user,'public.admin_role_grant','INSERT')
       OR has_table_privilege(current_user,'public.admin_permission_request','UPDATE')
       OR has_table_privilege(current_user,'public.admin_audit','UPDATE') THEN
        RAISE EXCEPTION 'application account has direct administrative write privileges';
    END IF;

    IF has_table_privilege(current_user,'public.schema_migration','SELECT') IS DISTINCT FROM true
       OR has_table_privilege(current_user,'public.schema_migration','INSERT')
       OR has_table_privilege(current_user,'public.schema_migration','UPDATE')
       OR has_table_privilege(current_user,'public.schema_migration','DELETE') THEN
        RAISE EXCEPTION 'application account migration status access is not read-only';
    END IF;

    FOREACH function_signature IN ARRAY ARRAY[
        'public.ecr_issue_admin_grant(bigint,bigint,text,text,text[],bigint,bigint,text)',
        'public.ecr_revoke_admin_grant(bigint,bigint,bigint,text)',
        'public.ecr_decide_admin_request(bigint,bigint,bigint,text,text)',
        'public.ecr_check_admin_grant(bigint,bigint,text,text)'
    ] LOOP
        IF has_function_privilege(current_user,function_signature,'EXECUTE') IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'application account cannot execute %', function_signature;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_proc p
            CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) acl
            WHERE p.oid=function_signature::regprocedure AND acl.grantee=0
              AND acl.privilege_type='EXECUTE'
        ) THEN
            RAISE EXCEPTION 'PUBLIC can execute %', function_signature;
        END IF;
    END LOOP;
END $$;

DO $$
DECLARE
    tenant_key BIGINT;
    job_key BIGINT;
    version_key BIGINT;
    item_key BIGINT;
    rejected BOOLEAN := false;
BEGIN
    SELECT j.tenant_id,j.id,i.document_version_id
      INTO tenant_key,job_key,version_key
      FROM ingest_job j
      JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
      JOIN ingest_job_item i ON i.tenant_id=j.tenant_id AND i.job_id=j.id
     WHERE s.type='admin_manifest' AND i.document_version_id IS NOT NULL
     ORDER BY j.id DESC LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'an isolated admin package is required to verify fencing';
    END IF;

    INSERT INTO ingest_job_item(tenant_id,job_id,data_id,payload_hash,status,owner_token,fencing_epoch,
                                lease_expires_at,cancel_requested,stage,document_version_id)
    VALUES (tenant_key,job_key,'fencing-smoke-'||txid_current()::text,repeat('a',64),'running',
            'current-worker',7,now()+interval '1 minute',false,'fencing-smoke',version_key)
    RETURNING id INTO item_key;

    BEGIN
        INSERT INTO ingest_publication(tenant_id,item_id,fencing_epoch,document_version_id)
        VALUES (tenant_key,item_key,6,version_key);
        RAISE EXCEPTION 'stale worker publication unexpectedly succeeded';
    EXCEPTION WHEN SQLSTATE '55000' THEN
        rejected := true;
    END;
    IF NOT rejected THEN
        RAISE EXCEPTION 'stale worker fencing token was not rejected';
    END IF;
END $$;

SELECT 'PASS admin privileges, job status, and stale worker fencing' AS check;
ROLLBACK;
