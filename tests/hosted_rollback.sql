begin;
set local lock_timeout='2s';
set local statement_timeout='10s';
create temporary table fixture_result (value jsonb) on commit drop;
do $fixture$
declare ws uuid; first jsonb; retry jsonb; conflict jsonb; stale jsonb; fixture jsonb := $metadata${"classification_code": "fixture-evidence", "content_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "department": "operations", "derived_view_version": 1, "event_id": "event-1", "evidence_needed": [], "evidence_tier": "synthetic", "family_id": "fixture-family", "kind": "definition", "occurrence_id": "occurrence-1", "reason_code": null, "receipt_sha256": null, "record_version": 1, "schema_version": 1, "source_manifest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "sources": [{"id": "fixture-source", "observed_at": "2026-10-11T11:36:14Z", "revision": 1, "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}], "state": "defined", "supersedes_event_id": null, "workflow_event_version": 1, "workflow_id": "fixture-workflow"}$metadata$::jsonb;
begin
 insert into public.rsi_workspaces(name) values('task8-rollback-only-workflow-fixture') returning id into ws;
 first := public.rsi_commit(ws,'fixture-owner','fixture-request-1','orgwf:event-1',0,fixture);
 if (first->'event'->'record'->>'revision')::int<>1 or first->>'replayed'<>'false' then raise exception 'initial fixture receipt invalid';end if;
 retry := public.rsi_commit(ws,'fixture-owner','fixture-request-1','orgwf:event-1',0,fixture);
 if retry->>'replayed'<>'true' or retry->'event'<>first->'event' then raise exception 'retry differs';end if;
 conflict := public.rsi_commit(ws,'fixture-owner','fixture-request-1','orgwf:event-1',0,fixture||'{"department":"unknown"}'::jsonb);
 if (conflict->>'_status')::int<>409 then raise exception 'changed retry accepted';end if;
 stale := public.rsi_commit(ws,'fixture-owner','fixture-request-2','orgwf:event-1',0,fixture);
 if (stale->>'_status')::int<>409 then raise exception 'stale write accepted';end if;
 if (select value from public.rsi_records where workspace=ws and key='orgwf:event-1')<>fixture then raise exception 'record readback differs';end if;
 if (select count(*) from public.rsi_events where workspace=ws)<>1 then raise exception 'duplicate event';end if;
 if exists(select 1 from public.rsi_keys where workspace=ws) then raise exception 'unexpected key creation';end if;
 insert into fixture_result values(jsonb_build_object('kind','rollback_only_live_database_core','first_receipt',first,'retry_same',true,'changed_retry_status',409,'stale_revision_status',409,'record_readback_equal',true,'keys_created',0,'page',jsonb_build_object('events',jsonb_build_array(first->'event'),'next_cursor',1,'high_watermark',1,'has_more',false)));
end $fixture$;
select value from fixture_result;
rollback;
