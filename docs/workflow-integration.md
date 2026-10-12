# Organization workflow recovery contract v1

This optional Python library is an **offline metadata recovery cache**, not a workflow executor, private hosted integration, approval system, or new MCP tool. Existing four public MCP tools and package registration remain unchanged. It uses SQLite from the Python standard library; no new service, credentials, permissions, or migrations are introduced.

The existing public notebook connector cannot receive private organization content. Private Shared Memory/RSI workspaces already expose versioned records and ordered replay, but their `_rsi:checkpoint` records acknowledge release adoption rather than business-task completion. A future authorized client can supply existing `/v1/events/` pages to `RecoveryCache`; this change performs no network calls or imports.

## Minimized record contract

Store each immutable metadata event under `orgwf:EVENT_ID` using the existing `/v1/commands/` envelope: `expected_revision: 0`, stable `request_id`, and the strict value below. Never revise that event key. A correction gets a new event ID and `supersedes_event_id`; the original remains present. Backend ordinary records are generic and do not enforce this convention server-side. All writers would need to agree to this contract before adoption.

| Field | Meaning |
| --- | --- |
| `schema_version` | Exactly 1; unknown versions hold without migration |
| `workflow_id`, `record_version`, `content_sha256` | Stable canonical identity, positive canonical definition version, digest of independently retained minimized definition |
| `workflow_event_version` | Consecutive per-workflow metadata event ordinal; separate from canonical definition version |
| `derived_view_version` | Positive independent classification/view version |
| `department`, `family_id`, `classification_code` | Department enum, family reference, rationale reference/code; no guessed classification |
| `source_manifest_sha256`, `sources` | Pinned source/window manifest digest; 1–32 opaque source IDs with positive revisions, SHA-256 and UTC `observed_at` |
| `evidence_tier` | `synthetic`, `historical`, `reported`, `verified`; a label is a contributor assertion |
| `event_id`, `occurrence_id` | Immutable event and distinct occurrence identity; no inferred recurrence |
| `kind`, `state` | definition/request/attempt/receipt/correction/hold, with defined/requested/attempted/confirmed/failed/unknown/held states |
| `supersedes_event_id` | Null or exact original event in this workflow |
| `reason_code`, `evidence_needed` | Held/unknown require explicit reason and opaque evidence-needed references |
| `receipt_sha256` | Required for confirmed/failed receipt; hash is a claimed link, not verification of external effect |

Departments: `sales_marketing`, `research_development`, `operations`, `customer_support`, `finance_admin`, `legal_compliance`, `unknown`. These are available categories, not claims that all departments occur in Sean's corpus. Cross-department handoffs use distinct event/occurrence records and source references; a typed handoff schema and alias map remain to be agreed with the coordinator. Do not derive frequency or ROI from this cache.

Allowed strings are bounded identifier codes, digests, enums and exact UTC timestamps, including fractional seconds. No arbitrary body, raw source text, URL, credential, saved permission or approval field is accepted. **This is minimization, not a DLP guarantee**: the caller must review IDs, source metadata and content digests before use. Never feed confidential client content, personal conversations, tax/financial material or credentials, even inside apparently structured fields. `finance_admin` is a category option, not permission to import financial records.

## Replay and fresh-session recovery

```python
from shared_memory_mcp.recovery import RecoveryCache

identity = {
    "workspace_id": "verified-workspace-id",
    "history_id": "operator-pinned-restore-epoch",
    "source_manifest_sha256": "a" * 64,
}
cache = RecoveryCache("reviewed-metadata.sqlite", **identity)
# page must be an authenticated response from this exact workspace.
checkpoint = cache.apply_page(page, **identity)
# Independently persist checkpoint only after apply_page returns successfully.
# A new process reopens the same cache after conversational context loss.
context = cache.recover(
    "stable-workflow-id", checkpoint=checkpoint,
    current_workspace_id=identity["workspace_id"],
    current_scope="read",  # obtained by fresh authenticated workspace_info
    current_source_manifest_sha256=identity["source_manifest_sha256"],
)
assert context["action_authorized"] is False
```

Replay must start at sequence 0 for a new cache and include all workspace events in order. Non-`orgwf:` values are hashed and discarded, not copied to local storage. Actor/request/record IDs remain in the local replay ledger and must also be reviewed/minimized. Sequence gaps, changed retries, version gaps, inconsistent page cursors, source regression, mixed tenants or manifests fail atomically. Duplicate identical pages are safe. Cursor and metadata commit in one SQLite transaction with `synchronous=FULL`.

A page advertising remaining backlog, missing workflow, unknown department, pending occurrence or more than 32 returned workflow metadata records makes recovered context **held**. A receipt closes only its own occurrence; no receipt means the execution outcome stays unresolved. On uncertain external effect, reconcile a real receipt with the original request/attempt; do not issue a new action merely because conversation history disappeared.

Fresh workspace authorization and source manifest are supplied separately on **every** recovery; they are never stored as durable authority. Revoked/expired/missing scopes fail closed. Historical approvals cannot enter this schema, and recovery never authorizes any action, even with a current owner scope. The external runtime must revalidate authority immediately before an action and retain real request/attempt/receipt evidence. These scope arguments are caller assertions, not an authentication implementation.

The hosted API does not expose a database restore epoch. `history_id` must be independently pinned and changed after restoration. High-watermark regression detects some older restores, but same-length divergent histories need independent source verification. A checkpoint independently retained outside the cache detects missing/stale/truncated cache state. Losing both copies cannot be detected locally; stop and rebuild from verified source history. An arbitrary cutoff of previously acknowledged source history is not simulated by an LLM context-window test.

## Backup, limits and audit

`cache.backup(new_path)` uses SQLite's consistent online backup and returns the checkpoint of the copied snapshot. It refuses to overwrite an existing path. Reopen the copy with the same identity and compare an independently retained checkpoint. Restoring an older backup against a newer checkpoint holds. Unknown schema versions are rejected; there is no destructive migration.

The cache retains immutable originals, explicit source versions, event fingerprints and a chained replay checkpoint. Recovery checks stored metadata digests and event chain continuity. These are accidental corruption checks, not signed provenance or protection against an attacker who can edit the cache and its checkpoint. Local filesystem protection/encryption/retention are outside this implementation. It is not suitable for secrets or regulated data.

Input pages are capped at 100 events; metadata is capped at 12,000 encoded bytes. Context returns at most 32 records. Replay history has no retention or compaction in v1; audit and occurrence reconciliation scan local history. The 1,000-event measurement is a tiny synthetic test, not a performance SLA or proof of organization autonomy.

## Adoption gates

1. Coordinator finalizes the minimized canonical-ID/alias, department, view-version, source-window and handoff manifest. No interim task-7 classifications are imported.
2. Identify an already authorized private workspace and permitted source scope; independently verify backend restore epoch/backup arrangements and current keys without changing access.
3. Agree immutable event writers and current-authority/receipt reconciliation outside this library, then run a separately authorized private fixture read/write/readback exercise.
4. Ship a reviewed compatible package release through the existing release workflow. The optional component release does not establish a hosted durable organization integration.

## Independent component release and bounded backend check

The optional offline cache can be released independently of a private workflow integration. No network/authentication behavior or MCP tool changes are needed for that component release. A new package/manifest/registry version, new Git tag, tested `.mcpb` bundle and matching registry SHA-256 must be prepared together; never replace the existing immutable v1.1.0 bundle or reuse its digest. Registry publication currently runs manually from main after it verifies the released bundle. A draft PR is implementation/review evidence, not a published package or production integration.

`tests/hosted_rollback.sql` is an optional, bounded database-core fixture for an **already authorized management connection**. It creates one synthetic workspace inside an explicit transaction, creates no keys, stores one synthetic metadata event, checks readback/exact retry/changed retry/stale revision, then rolls back. It does not run in normal CI or via the MCP tools, and it does not test private HTTP authentication. Independently confirm zero fixture rows afterward. Passing its event envelope into the offline cache verifies core/cache compatibility, not persistence of an HTTP workflow.

An HTTP fixture requires an existing authorized private workspace and current owner/writer key. If no such workspace exists, do not use the managed public notebook as a substitute, mint credentials, or bypass its scope controls. Any separately approved creation of a disposable private fixture must identify its sanitized records, API routes, bounded operation count and cleanup; workspace deletion has no normal undo, while record-only cleanup cannot erase append-only history.

## Schema 2.0.0 reference adapter (1.1.2)

`schema2_reference.bind_reference` attaches optional `source_context`: the exact contract, dataset record, alias map and hold-register SHA-256/JSON pointers, plus explicit unknown-boundary status. It does not read, copy, ingest or canonicalize any source artifact. Exact original evidence tiers and occurrence/report-date precision remain in that immutable referenced source; the coarse cache evidence label is not a replacement for them. If an original artifact cannot be verified/read within current source permissions, the external runtime must hold rather than reconstruct a stronger claim.

Source `observed_at` accepts and retains fractional UTC timestamps without rounding. This is a source-observation timestamp, not an inferred occurrence time. Unknown episode boundaries require explicit unknown/held state with reason/evidence needed. No aliases or holds are automatically merged/resolved; no current authority is loaded from historical data. Older cache values continue to validate, with no database migration. This adapter adds no MCP tools and does not make them available to root without separately verified installation/access.

## Recovery correctness patch (1.1.3)

Each `(workflow_id, canonical record_version)` maps to one immutable content digest; event ordinals remain separate. Both replay and recovery reject conflicting canonical identities, including conflicts in an older cache, without rewriting originals or running a migration.

Only request/attempt/receipt records track execution occurrences. Metadata corrections do not create pending executions. Holds are tracked independently and cannot be cleared by a receipt. An explicit metadata correction must supersede the exact hold, use `state=defined`, supply the `hold_resolved` reason (or `boundary_resolved` with explicit source-context unknown_boundary=false), and retain evidence source references. This records an assertion; it does not independently verify the claim or grant current authority.

Recovery returns at most 32 unresolved occurrence details and 32 hold details, with total counts and separate truncation flags; any omitted details keep status held/truncated. JSON Pointer tokens allow only valid ~0/~1 escapes. Raw source bodies, saved approvals and credentials remain forbidden.

Hold resolution preserves all four original schema-2 hash/pointer references exactly and retains every original source identity, revision, digest and timestamp. Additional evidence is allowed; unrelated corrections remain historical metadata and cannot clear the hold. Recovery applies this rule to existing caches too.
