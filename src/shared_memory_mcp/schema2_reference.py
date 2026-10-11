"""Bind reviewed metadata to exact schema-2 source references without importing it.

The immutable referenced artifact retains original evidence tiers and occurrence-date
precision. This adapter neither loads that artifact nor collapses its claims into a
verified outcome. Original source availability/authority must be checked separately.
"""
from copy import deepcopy
from .recovery import validate_metadata


def bind_reference(metadata, *, contract_sha256, dataset_sha256, alias_sha256,
                   record_pointer, holds_pointer, unknown_boundary):
    value = deepcopy(metadata)
    value['source_context'] = {
        'schema_version': '2.0.0',
        'contract_ref': {'sha256': contract_sha256, 'json_pointer': ''},
        'record_ref': {'sha256': dataset_sha256, 'json_pointer': record_pointer},
        'alias_ref': {'sha256': alias_sha256, 'json_pointer': '/canonical_alias_map'},
        'holds_ref': {'sha256': dataset_sha256, 'json_pointer': holds_pointer},
        'unknown_boundary': unknown_boundary,
    }
    return validate_metadata(value)
