"""Offline synthetic compatibility decisions; no model runtime or snapshot I/O."""
import copy
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from shared_memory_mcp.recovery import RecoveryCache, RecoveryError, digest
from shared_memory_mcp.runtime_state import (
    RuntimeIdentity, RuntimeStateReference, recover_with_runtime_state,
)
from test_recovery import IDENTITY, event, metadata, page


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = RecoveryCache(Path(self.temp.name) / 'cache.sqlite', **IDENTITY)
        self.addCleanup(self.cache.close)
        self.cache.apply_page(page([event()]), **IDENTITY)
        self.recovery_args = dict(
            checkpoint=self.cache.checkpoint(), current_workspace_id=IDENTITY['workspace_id'],
            current_scope='read', current_source_manifest_sha256='a' * 64)
        self.runtime = RuntimeIdentity(
            model_id='synthetic-model', model_sha256='1' * 64, adapter_sha256='2' * 64,
            tokenizer_sha256='3' * 64, runtime_id='synthetic-runtime', runtime_version='1.0',
            cache_format='synthetic-kv', cache_version='1', position_sha256='4' * 64,
            config_sha256='5' * 64)
        self.context = self.cache.recover('fixture-workflow', **self.recovery_args)
        self.ref = RuntimeStateReference(
            schema_version=1, artifact_id='opaque-artifact-1', artifact_sha256='6' * 64,
            runtime=self.runtime, task_id='task-1', workspace_id=IDENTITY['workspace_id'],
            workflow_id='fixture-workflow', recovery_sha256=digest(self.context),
            permission_context_sha256='7' * 64, expires_at='2026-10-20T00:00:00Z')
        self.args = dict(
            reference=self.ref, reference_sha256=digest(self.ref.to_dict()),
            current_runtime=self.runtime, current_task_id='task-1',
            current_permission_context_sha256='7' * 64, observed_artifact_sha256='6' * 64,
            now=datetime(2026, 10, 19, tzinfo=timezone.utc))

    def recover(self, **overrides):
        return recover_with_runtime_state(
            self.cache, 'fixture-workflow', **{**self.recovery_args, **self.args, **overrides})

    def assert_fallback(self, reason, **overrides):
        result = self.recover(**overrides)
        self.assertEqual(result['runtime_state']['status'], 'evidence_only')
        self.assertIn(reason, result['runtime_state']['reasons'])
        self.assertIsNone(result['runtime_state']['artifact_id'])
        self.assertFalse(result['context']['action_authorized'])
        self.assertFalse(result['runtime_state']['action_authorized'])
        return result

    def test_legacy_recovery_unchanged_and_no_reference_falls_back(self):
        result = self.assert_fallback('runtime_state_absent', reference=None)
        self.assertEqual(result['context'], self.context)
        self.assertNotIn('runtime_state', self.context)

    def test_matching_metadata_only_allows_external_validation(self):
        result = self.recover(reference=self.ref.to_dict())
        self.assertEqual(result['context'], self.context)
        self.assertEqual(result['runtime_state'], dict(
            status='eligible_for_external_validation', reasons=[],
            artifact_id='opaque-artifact-1', action_authorized=False))

    def test_every_runtime_identity_component_must_match(self):
        for field in fields(RuntimeIdentity):
            with self.subTest(field=field.name):
                changed = 'f' * 64 if field.name.endswith('_sha256') else 'different-version'
                self.assert_fallback(field.name + '_mismatch', current_runtime=replace(
                    self.runtime, **{field.name: changed}))

    def test_missing_current_identity_and_artifact_verification(self):
        self.assert_fallback('current_runtime_identity_required', current_runtime=None)
        for observed in (None, 'f' * 64, 'invalid'):
            self.assert_fallback('artifact_integrity_unverified', observed_artifact_sha256=observed)

    def test_expired_state_at_exact_boundary(self):
        for day in (20, 21):
            self.assert_fallback('runtime_state_expired', now=datetime(2026, 10, day, tzinfo=timezone.utc))
        self.assert_fallback('current_time_required', now=datetime(2026, 10, 19))

    def test_changed_evidence_invalidates_exact_snapshot(self):
        changed = metadata(2)
        changed['sources'][0]['revision'] = 2
        changed['sources'][0]['sha256'] = 'e' * 64
        cp = self.cache.apply_page(page([event(2, changed)]), **IDENTITY)
        self.assert_fallback('source_snapshot_mismatch', checkpoint=cp)

    def test_scope_and_permission_invalidation(self):
        for task in (None, 'another-task'):
            self.assert_fallback('task_scope_mismatch', current_task_id=task)
        for permission in (None, 'e' * 64):
            self.assert_fallback('permission_context_mismatch', current_permission_context_sha256=permission)
        for name in ('workspace_id', 'workflow_id'):
            ref = replace(self.ref, **{name: 'other'})
            self.assert_fallback('workspace_or_workflow_scope_mismatch', reference=ref,
                                 reference_sha256=digest(ref.to_dict()))

    def test_invalid_or_corrupt_reference_falls_back_without_losing_context(self):
        for bad in ([], 'raw snapshot', {}, {'schema_version': 2}):
            self.assert_fallback('invalid_runtime_reference', reference=bad)
        mutations = [dict(schema_version=2), dict(schema_version=True),
                     dict(expires_at='2026-02-30T00:00:00Z'), dict(artifact_id='https://invalid.test'),
                     dict(action_authorized=True), dict(artifact_sha256=[])]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                bad = {**self.ref.to_dict(), **mutation}
                result = self.assert_fallback('invalid_runtime_reference', reference=bad)
                self.assertEqual(result['context'], self.context)
        bad = copy.deepcopy(self.ref.to_dict())
        bad['runtime']['unrecognized_setting'] = True
        self.assert_fallback('invalid_runtime_reference', reference=bad)
        bad = replace(self.ref, artifact_id='changed')
        self.assert_fallback('runtime_reference_integrity_unverified', reference=bad)
        self.assert_fallback('runtime_reference_integrity_unverified', reference_sha256=None)

    def test_unresolved_actions_block_even_matching_snapshot(self):
        for kind, state in (('request', 'requested'), ('attempt', 'attempted')):
            cp = self.cache.apply_page(page([event(2 if kind == 'request' else 3,
                metadata(2 if kind == 'request' else 3, kind, state))]), **IDENTITY)
            self.recovery_args['checkpoint'] = cp
            context = self.cache.recover('fixture-workflow', **self.recovery_args)
            ref = replace(self.ref, recovery_sha256=digest(context))
            self.assert_fallback('unresolved_actions_require_reconciliation', reference=ref,
                                 reference_sha256=digest(ref.to_dict()))

    def test_held_evidence_blocks_matching_reference(self):
        held = metadata(2, 'hold', 'held')
        held.update(reason_code='needs-evidence', evidence_needed=['receipt'])
        self.recovery_args['checkpoint'] = self.cache.apply_page(page([event(2, held)]), **IDENTITY)
        context = self.cache.recover('fixture-workflow', **self.recovery_args)
        ref = replace(self.ref, recovery_sha256=digest(context))
        self.assert_fallback('evidence_recovery_held', reference=ref, reference_sha256=digest(ref.to_dict()))

    def test_invalid_evidence_and_revoked_access_still_raise(self):
        for scope in (None, 'revoked', 'expired'):
            with self.assertRaisesRegex(RecoveryError, 'fresh_access'):
                self.recover(current_scope=scope)
        with self.assertRaisesRegex(RecoveryError, 'checkpoint'):
            self.recover(checkpoint={})
        self.cache.db.execute("UPDATE metadata SET value='{}'")
        with self.assertRaisesRegex(RecoveryError, 'integrity'):
            self.recover()


if __name__ == '__main__':
    unittest.main()
