"""Synthetic, offline recovery tests. No external requests or real organization data."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from shared_memory_mcp.recovery import RecoveryCache, RecoveryError, digest

IDENTITY = dict(workspace_id='synthetic-workspace', history_id='fixture-epoch-1', source_manifest_sha256='a'*64)

def metadata(n=1, kind='definition', state='defined', occurrence='occurrence-1'):
    return dict(schema_version=1, workflow_id='fixture-workflow', record_version=1, workflow_event_version=n,
                content_sha256='b'*64, derived_view_version=1, department='operations',
                family_id='fixture-family', classification_code='fixture-evidence',
                source_manifest_sha256='a'*64, sources=[dict(id='fixture-source', revision=1, sha256='c'*64, observed_at='2026-10-11T11:36:14Z')],
                evidence_tier='synthetic', event_id=f'event-{n}', occurrence_id=occurrence,
                kind=kind, state=state, supersedes_event_id=None, reason_code=None,
                evidence_needed=[], receipt_sha256='d'*64 if kind=='receipt' else None)

def event(n=1, value=None):
    value = value if value is not None else metadata(n)
    return dict(sequence=n, actor='fixture-agent', request_id=f'request-{n}',
                record=dict(key='orgwf:'+value['event_id'], revision=1, value=value))

def page(events, watermark=None):
    seq=events[-1]['sequence'] if events else 0
    watermark = seq if watermark is None else watermark
    return dict(events=events, next_cursor=seq, high_watermark=watermark, has_more=seq<watermark)

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/'cache.sqlite'
        self.cache=RecoveryCache(self.path, **IDENTITY)
    def tearDown(self):
        self.cache.close(); self.temp.cleanup()
    def apply(self, events, watermark=None):
        return self.cache.apply_page(page(events, watermark), **IDENTITY)
    def recover(self, **kw):
        args=dict(checkpoint=self.cache.checkpoint(), current_workspace_id=IDENTITY['workspace_id'], current_scope='read', current_source_manifest_sha256='a'*64)
        args.update(kw); return self.cache.recover('fixture-workflow', **args)
    def test_fresh_process_after_context_loss(self):
        cp=self.apply([event()]); self.cache.close()
        code="from shared_memory_mcp.recovery import RecoveryCache; import json,sys; c=RecoveryCache(sys.argv[1],**json.loads(sys.argv[2])); print(json.dumps(c.recover('fixture-workflow', checkpoint=json.loads(sys.argv[3]),current_workspace_id='synthetic-workspace', current_scope='read',current_source_manifest_sha256='a'*64)))"
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
        result=subprocess.run([sys.executable,'-c',code,str(self.path),json.dumps(IDENTITY),json.dumps(cp)],env=env,capture_output=True,text=True,check=True)
        data=json.loads(result.stdout); self.assertEqual(data['records'][0]['event_id'],'event-1'); self.assertFalse(data['action_authorized'])
        self.cache=RecoveryCache(self.path, **IDENTITY)
    def test_missing_workflow_is_held(self):
        self.assertEqual(self.recover()['status'],'held')
    def test_identical_retries_do_not_duplicate(self):
        cp=self.apply([event()])
        for _ in range(100): self.assertEqual(cp,self.apply([event()]))
        self.assertEqual(self.cache.db.execute('SELECT count(*) FROM metadata').fetchone()[0],1)
    def test_conflicting_retry(self):
        self.apply([event()]); e=event(); e['record']['value']['department']='unknown'
        with self.assertRaisesRegex(RecoveryError,'conflicting_replay'): self.apply([e])
    def test_page_is_atomic_on_missing_event(self):
        before=self.cache.checkpoint()
        with self.assertRaisesRegex(RecoveryError,'missing_events'): self.apply([event(1),event(3)])
        self.assertEqual(self.cache.checkpoint(),before)
    def test_cursor_not_last_attempt(self):
        p=page([event()]); p['next_cursor']=2
        with self.assertRaisesRegex(RecoveryError,'cursor'): self.cache.apply_page(p,**IDENTITY)
        self.assertEqual(self.cache.checkpoint()['last_durable_sequence'],0)
    def test_stale_checkpoint(self):
        stale=self.apply([event()]); self.apply([event(2)])
        with self.assertRaisesRegex(RecoveryError,'checkpoint'): self.recover(checkpoint=stale)
    def test_tenant_history_scope_mismatch(self):
        for key,value in [('workspace_id','other'),('history_id','other'),('source_manifest_sha256','e'*64)]:
            identity={**IDENTITY,key:value}
            with self.assertRaises(RecoveryError): self.cache.apply_page(page([event()]),**identity)
    def test_source_regression(self):
        self.apply([event()])
        with self.assertRaisesRegex(RecoveryError,'regressed'): self.cache.apply_page(page([]),**IDENTITY)
    def test_fresh_permissions_required_even_after_reopen(self):
        self.apply([event()]); self.cache.close(); self.cache=RecoveryCache(self.path,**IDENTITY)
        for scope in [None,'revoked','expired','historical_owner']:
            with self.assertRaisesRegex(RecoveryError,'fresh_access'): self.recover(current_scope=scope)
    def test_approval_and_raw_content_excluded(self):
        for field in ['body','api_key','approval','client_content','personal_conversation','tax_return']:
            v=metadata(); v[field]='must-never-be-stored'
            with self.assertRaisesRegex(RecoveryError,'metadata_fields'): self.apply([event(value=v)])
        self.assertEqual(self.cache.checkpoint()['last_durable_sequence'],0)
    def test_originals_immutable_and_corrections_linked(self):
        self.apply([event()]); v=metadata(2,'correction','defined'); v['supersedes_event_id']='event-1'
        self.apply([event(2,v)])
        data=self.recover(); self.assertEqual(len(data['records']),2)
        self.assertEqual(data['records'][1],metadata())
        e=event(3); e['record']['key']='orgwf:event-1'; e['record']['revision']=2
        with self.assertRaisesRegex(RecoveryError,'immutable'): self.apply([e])
    def test_missing_supersession_is_held(self):
        v=metadata(kind='correction'); v['supersedes_event_id']='missing'
        with self.assertRaisesRegex(RecoveryError,'original'): self.apply([event(value=v)])
    def test_ambiguous_outcome_requires_receipt(self):
        v=metadata(kind='attempt',state='confirmed')
        with self.assertRaisesRegex(RecoveryError,'receipt'): self.apply([event(value=v)])
    def test_attempt_survives_restart_without_becoming_success(self):
        self.apply([event(1,metadata(1,'request','requested')),event(2,metadata(2,'attempt','attempted'))])
        self.cache.close(); self.cache=RecoveryCache(self.path,**IDENTITY)
        r=self.recover(); self.assertEqual(r['status'],'held'); self.assertEqual(r['unresolved_occurrences'][0]['state'],'attempted')
    def test_receipt_does_not_close_other_occurrence(self):
        self.apply([event(1,metadata(1,'attempt','attempted','first')),event(2,metadata(2,'attempt','attempted','second')),event(3,metadata(3,'receipt','confirmed','second'))])
        self.assertEqual(self.recover()['unresolved_occurrences'][0]['occurrence_id'],'first')
    def test_held_state_requires_evidence_needed(self):
        v=metadata(kind='hold',state='held')
        with self.assertRaisesRegex(RecoveryError,'unexplained_hold'): self.apply([event(value=v)])
        v['reason_code']='receipt_missing';v['evidence_needed']=['receipt-hash']
        self.apply([event(value=v)]); self.assertEqual(self.recover()['status'],'held')
    def test_unknown_schema_no_destructive_migration(self):
        v=metadata();v['schema_version']=2
        with self.assertRaisesRegex(RecoveryError,'schema'): self.apply([event(value=v)])
        self.cache.db.execute('UPDATE meta SET version=2'); self.cache.close()
        with self.assertRaisesRegex(RecoveryError,'schema'): RecoveryCache(self.path,**IDENTITY)
        self.cache=RecoveryCache.__new__(RecoveryCache);self.cache.db=__import__('sqlite3').connect(self.path)
        self.assertEqual(self.cache.db.execute('SELECT version FROM meta').fetchone()[0],2)
    def test_concurrent_retries(self):
        def write(_):
            c=RecoveryCache(self.path,**IDENTITY)
            try:return c.apply_page(page([event()]),**IDENTITY)
            finally:c.close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(write,range(32)))
        self.assertTrue(all(r==results[0] for r in results)); self.assertEqual(self.cache.checkpoint()['last_durable_sequence'],1)
    def test_concurrent_conflicting_writers(self):
        def write(n):
            c=RecoveryCache(self.path,**IDENTITY);v=metadata();v['classification_code']=f'classification-{n}'
            try:c.apply_page(page([event(value=v)]),**IDENTITY);return True
            except RecoveryError:return False
            finally:c.close()
        with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(write,range(16)))
        self.assertEqual(sum(results),1)
    def test_backup_restore_and_older_backup_detection(self):
        cp=self.apply([event()]); backup=Path(self.temp.name)/'backup.sqlite'; self.cache.backup(backup)
        self.apply([event(2)]); newer=self.cache.checkpoint(); restored=RecoveryCache(backup,**IDENTITY)
        try:
            with self.assertRaisesRegex(RecoveryError,'checkpoint'): restored.recover('fixture-workflow',checkpoint=newer)
            self.assertEqual(restored.checkpoint(),cp)
        finally:restored.close()
        with self.assertRaises(FileExistsError): self.cache.backup(backup)
    def test_tampered_metadata(self):
        self.apply([event()]);v=metadata();v['department']='unknown'
        self.cache.db.execute('UPDATE metadata SET value=?',(json.dumps(v),))
        with self.assertRaisesRegex(RecoveryError,'integrity'):self.recover()
    def test_unrelated_payload_not_retained(self):
        e=event();e['record']={'key':'other-record','revision':1,'value':{'private':'excluded-fixture'}}
        self.apply([e]); self.assertEqual(self.cache.db.execute('SELECT count(*) FROM metadata').fetchone()[0],0)
        self.assertNotIn(b'excluded-fixture',self.path.read_bytes())
    def test_duplicate_actor_request_conflict(self):
        self.apply([event()]);e=event(2);e['request_id']='request-1'
        with self.assertRaisesRegex(RecoveryError,'duplicate_identity'):self.apply([e])
    def test_truncated_context_is_held(self):
        self.apply([event(n) for n in range(1,35)])
        r=self.recover();self.assertTrue(r['truncated']);self.assertEqual(r['status'],'held');self.assertEqual(len(r['records']),32)
    def test_missing_record_version(self):
        e=event();e['record']['revision']=2
        with self.assertRaisesRegex(RecoveryError,'record_version'):self.apply([e])
    def test_backlogged_page_cannot_recover_complete_context(self):
        self.apply([event()],watermark=2)
        self.assertEqual(self.recover()['status'],'held')
        self.apply([event(2)])
        self.assertEqual(self.recover()['status'],'historical_context')
    def test_orphan_receipt(self):
        with self.assertRaisesRegex(RecoveryError,'requires_attempt'):
            self.apply([event(value=metadata(kind='receipt',state='confirmed'))])
    def test_unknown_department_is_held(self):
        v=metadata();v['department']='unknown';self.apply([event(value=v)])
        self.assertEqual(self.recover()['status'],'held')
    def test_sigkill_mid_transaction_preserves_acknowledged_checkpoint(self):
        cp=self.apply([event()]); self.cache.close()
        code="""import json,os,signal,sys
from shared_memory_mcp import recovery
c=recovery.RecoveryCache(sys.argv[1],**json.loads(sys.argv[2]))
original=recovery.digest
def crash(value):
    if isinstance(value,dict) and value.get('sequence')==2 and 'previous' in value:
        os.kill(os.getpid(),signal.SIGKILL)
    return original(value)
recovery.digest=crash
c.apply_page(json.loads(sys.argv[3]),**json.loads(sys.argv[2]))
"""
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
        for _ in range(10):
            result=subprocess.run([sys.executable,'-c',code,str(self.path),json.dumps(IDENTITY),json.dumps(page([event(2)]))],env=env,capture_output=True)
            self.assertEqual(result.returncode,-9)
            self.cache=RecoveryCache(self.path,**IDENTITY);self.assertEqual(self.cache.checkpoint(),cp)
            self.assertEqual(len(self.recover()['records']),1);self.cache.close()
        self.cache=RecoveryCache(self.path,**IDENTITY);self.apply([event(2)])
        self.assertEqual(self.cache.checkpoint()['last_durable_sequence'],2)
    def test_missing_durable_metadata_is_detected(self):
        self.apply([event()]);self.cache.db.execute('DELETE FROM metadata')
        with self.assertRaisesRegex(RecoveryError,'integrity'): self.recover()
    def test_full_history_baseline_exact_version_pending_and_authority(self):
        history=[event(1,metadata()),event(2,metadata(2,'attempt','attempted','pending')),event(3,metadata(3,'attempt','attempted','finished')),event(4,metadata(4,'receipt','confirmed','finished'))]
        baseline=[e['record']['value'] for e in history]
        latest_by_occurrence={}
        for v in baseline:
            if v['kind']!='definition':latest_by_occurrence[v['occurrence_id']]=v
        expected_pending={k for k,v in latest_by_occurrence.items() if v['state'] not in {'confirmed','failed'}}
        self.apply(history);self.cache.close();self.cache=RecoveryCache(self.path,**IDENTITY)
        result=self.recover(current_scope='owner')
        self.assertEqual(result['records'],list(reversed(baseline)))
        self.assertEqual({v['occurrence_id'] for v in result['unresolved_occurrences']},expected_pending)
        self.assertFalse(result['action_authorized'])
    def test_restricted_provenance_through_handoff_and_later_read(self):
        self.apply([event()]);v=metadata(2,'request','requested','handoff')
        v['source_manifest_sha256']='e'*64
        with self.assertRaisesRegex(RecoveryError,'source_scope'):self.apply([event(2,v)])
        with self.assertRaisesRegex(RecoveryError,'fresh_access'):
            self.recover(current_source_manifest_sha256='e'*64)
        self.assertEqual(self.cache.checkpoint()['last_durable_sequence'],1)
    def test_exact_fractional_timestamp_is_preserved(self):
        v=metadata();v['sources'][0]['observed_at']='2026-10-11T11:36:14.115795Z'
        self.apply([event(value=v)])
        self.assertEqual(self.recover()['records'][0]['sources'][0]['observed_at'],v['sources'][0]['observed_at'])
    def test_schema2_exact_references_preserve_originals_without_loading_them(self):
        from shared_memory_mcp.schema2_reference import bind_reference
        original=metadata();v=bind_reference(original,contract_sha256='a'*64,dataset_sha256='b'*64,alias_sha256='c'*64,record_pointer='/mapping/7',holds_pointer='/registered_holds',unknown_boundary=False)
        self.assertNotIn('source_context',original)
        self.apply([event(value=v)])
        self.assertEqual(self.recover()['records'][0]['source_context'],v['source_context'])
    def test_schema2_unknown_boundary_cannot_become_complete(self):
        from shared_memory_mcp.schema2_reference import bind_reference
        kwargs=dict(contract_sha256='a'*64,dataset_sha256='b'*64,alias_sha256='c'*64,record_pointer='/mapping/7',holds_pointer='/registered_holds',unknown_boundary=True)
        with self.assertRaisesRegex(RecoveryError,'requires_hold'):bind_reference(metadata(),**kwargs)
        v=metadata(kind='hold',state='held');v['reason_code']='boundary_unknown';v['evidence_needed']=['boundary-evidence']
        bound=bind_reference(v,**kwargs);self.apply([event(value=bound)])
        self.assertEqual(self.recover()['status'],'held')
    def test_schema2_raw_content_is_rejected(self):
        from shared_memory_mcp.schema2_reference import bind_reference
        v=bind_reference(metadata(),contract_sha256='a'*64,dataset_sha256='b'*64,alias_sha256='c'*64,record_pointer='/mapping/7',holds_pointer='/registered_holds',unknown_boundary=False)
        v['source_context']['record_ref']['body']='forbidden-fixture-content'
        with self.assertRaisesRegex(RecoveryError,'reference'):self.apply([event(value=v)])
    def test_bad_timestamp(self):
        v=metadata();v['sources'][0]['observed_at']='2026-99-11T11:36:14Z'
        with self.assertRaisesRegex(RecoveryError,'timestamp'):self.apply([event(value=v)])

if __name__=='__main__': unittest.main(verbosity=2)
