"""Optional offline runtime-state metadata, never a snapshot loader or authority store.

All current inputs and independently retained hashes are caller assertions. Matching
metadata permits further external validation only; it cannot establish resumability.
"""
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone

from .recovery import HASH, digest, identifier


def _hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


@dataclass(frozen=True)
class RuntimeIdentity:
    """Exact immutable identities, including a digest for an empty adapter stack.

    config_sha256 covers all runtime-specific resume settings (dtype, quantization,
    parallel layout, architecture, etc.). position_sha256 covers the exact token
    prefix, positions, attention/RoPE settings and cache layout. IDs alone are not
    sufficient: versions and content hashes must identify the actual loaded assets.
    """

    model_id: str
    model_sha256: str
    adapter_sha256: str
    tokenizer_sha256: str
    runtime_id: str
    runtime_version: str
    cache_format: str
    cache_version: str
    position_sha256: str
    config_sha256: str

    def __post_init__(self):
        for field in fields(self):
            valid = _hash if field.name.endswith('_sha256') else identifier
            if not valid(getattr(self, field.name)):
                raise ValueError('invalid_runtime_identity')


@dataclass(frozen=True)
class RuntimeStateReference:
    """Small opaque reference stored separately from workflow metadata and facts."""

    schema_version: int
    artifact_id: str
    artifact_sha256: str
    runtime: RuntimeIdentity
    task_id: str
    workspace_id: str
    workflow_id: str
    recovery_sha256: str
    permission_context_sha256: str
    expires_at: str

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError('unsupported_runtime_reference_schema')
        if type(self.runtime) is not RuntimeIdentity:
            raise ValueError('invalid_runtime_identity')
        for name in ('artifact_id', 'task_id', 'workspace_id', 'workflow_id'):
            if not identifier(getattr(self, name)):
                raise ValueError('invalid_runtime_reference_scope')
        for name in ('artifact_sha256', 'recovery_sha256', 'permission_context_sha256'):
            if not _hash(getattr(self, name)):
                raise ValueError('invalid_runtime_reference_digest')
        _expiry(self.expires_at)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {f.name for f in fields(cls)}
                or type(value.get('runtime')) is not dict
                or set(value['runtime']) != {f.name for f in fields(RuntimeIdentity)}):
            raise ValueError('invalid_runtime_reference_fields')
        return cls(**{**value, 'runtime': RuntimeIdentity(**value['runtime'])})


def _expiry(value):
    # Fixed UTC format keeps this small extension deterministic and unambiguous.
    if not isinstance(value, str) or len(value) != 20:
        raise ValueError('invalid_runtime_expiry')
    try:
        parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError('invalid_runtime_expiry') from None
    if parsed.strftime('%Y-%m-%dT%H:%M:%SZ') != value:
        raise ValueError('invalid_runtime_expiry')
    return parsed


def recover_with_runtime_state(cache, workflow_id, *, reference=None,
                               reference_sha256=None, current_runtime=None,
                               current_task_id=None, current_permission_context_sha256=None,
                               observed_artifact_sha256=None, now=None, **recovery_args):
    """Recover evidence first, then conservatively assess an optional reference.

    recovery_args are passed unchanged to RecoveryCache.recover. Its failures still
    raise: an invalid evidence cache must never become a runtime-state fallback.
    reference_sha256 must be pinned independently of the reference; the observed
    artifact digest must come from an authorized external integrity check. Nothing
    here reads an artifact, authenticates access, restores state or replays actions.
    """
    context = cache.recover(workflow_id, **recovery_args)
    reasons = []
    if reference is None:
        reasons.append('runtime_state_absent')
    else:
        try:
            ref = RuntimeStateReference.from_dict(
                reference.to_dict() if type(reference) is RuntimeStateReference else reference)
        except (ValueError, TypeError):
            reasons.append('invalid_runtime_reference')
        else:
            if not _hash(reference_sha256) or digest(ref.to_dict()) != reference_sha256:
                reasons.append('runtime_reference_integrity_unverified')
            if type(current_runtime) is not RuntimeIdentity:
                reasons.append('current_runtime_identity_required')
            else:
                for field in fields(RuntimeIdentity):
                    if getattr(ref.runtime, field.name) != getattr(current_runtime, field.name):
                        reasons.append(field.name + '_mismatch')
            if not identifier(current_task_id) or ref.task_id != current_task_id:
                reasons.append('task_scope_mismatch')
            if ref.workspace_id != context['checkpoint']['workspace_id'] or ref.workflow_id != workflow_id:
                reasons.append('workspace_or_workflow_scope_mismatch')
            if ref.recovery_sha256 != digest(context):
                reasons.append('source_snapshot_mismatch')
            if (not _hash(current_permission_context_sha256)
                    or ref.permission_context_sha256 != current_permission_context_sha256):
                reasons.append('permission_context_mismatch')
            if not _hash(observed_artifact_sha256) or ref.artifact_sha256 != observed_artifact_sha256:
                reasons.append('artifact_integrity_unverified')
            clock = datetime.now(timezone.utc) if now is None else now
            if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
                reasons.append('current_time_required')
            elif clock >= _expiry(ref.expires_at):
                reasons.append('runtime_state_expired')
    if context['unresolved_occurrence_count']:
        reasons.append('unresolved_actions_require_reconciliation')
    if context['status'] != 'historical_context':
        reasons.append('evidence_recovery_held')
    decision = {
        'status': 'evidence_only' if reasons else 'eligible_for_external_validation',
        'reasons': reasons,
        'artifact_id': None if reasons else ref.artifact_id,
        'action_authorized': False,
    }
    return {'context': context, 'runtime_state': decision}
