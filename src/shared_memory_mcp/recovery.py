"""Offline recovery cache for reviewed, non-sensitive organization workflow metadata.

Consumes the existing RSI event envelope. No networking, credentials, action execution,
or approval storage. The source journal and current operator authority remain external.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
DEPARTMENTS = {"sales_marketing", "research_development", "operations",
               "customer_support", "finance_admin", "legal_compliance", "unknown"}
KINDS = {"definition", "request", "attempt", "receipt", "correction", "hold"}
STATES = {"defined", "requested", "attempted", "confirmed", "failed", "unknown", "held"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
FIELDS = {"schema_version", "workflow_id", "record_version", "workflow_event_version", "content_sha256",
          "derived_view_version", "department", "family_id", "classification_code",
          "source_manifest_sha256", "sources", "evidence_tier", "event_id",
          "occurrence_id", "kind", "state", "supersedes_event_id", "reason_code",
          "evidence_needed", "receipt_sha256"}


class RecoveryError(ValueError):
    """Recovery is held; do not advance the cursor or execute a workflow."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9007199254740991


def bind_source_versions(value, versions):
    """One immutable content digest per source ID/revision in this pinned cache."""
    for source in value["sources"]:
        identity = (source["id"], source["revision"])
        if versions.setdefault(identity, source["sha256"]) != source["sha256"]:
            raise RecoveryError("source_revision_conflict")


def validate_metadata(value):
    if not isinstance(value, dict) or not FIELDS <= set(value) or set(value) - FIELDS - {"source_context"}:
        raise RecoveryError("metadata_fields: raw content and authority fields are forbidden")
    if value["schema_version"] != SCHEMA_VERSION or type(value["schema_version"]) is not int:
        raise RecoveryError("unsupported_metadata_schema")
    for key in ("workflow_id", "family_id", "classification_code", "event_id", "occurrence_id"):
        if not identifier(value[key]):
            raise RecoveryError("invalid_identifier")
    for key in ("record_version", "workflow_event_version", "derived_view_version"):
        if not integer(value[key], 1):
            raise RecoveryError("invalid_version")
    for key in ("content_sha256", "source_manifest_sha256"):
        if not isinstance(value[key], str) or not HASH.fullmatch(value[key]):
            raise RecoveryError("invalid_digest")
    if value["department"] not in DEPARTMENTS or value["kind"] not in KINDS or value["state"] not in STATES:
        raise RecoveryError("invalid_classification_or_state")
    if value["evidence_tier"] not in {"synthetic", "historical", "reported", "verified"}:
        raise RecoveryError("invalid_evidence_tier")
    for key in ("supersedes_event_id", "reason_code"):
        if value[key] is not None and not identifier(value[key]):
            raise RecoveryError("invalid_reference")
    sources = value["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        raise RecoveryError("missing_source_scope")
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {"id", "revision", "sha256", "observed_at"}
                or not identifier(source["id"]) or not integer(source["revision"], 1)
                or not isinstance(source["sha256"], str) or not HASH.fullmatch(source["sha256"])
                or not isinstance(source["observed_at"], str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", source["observed_at"])):
            raise RecoveryError("invalid_source_reference")
        try:
            datetime.fromisoformat(source["observed_at"].removesuffix("Z") + "+00:00")
        except ValueError:
            raise RecoveryError("invalid_source_timestamp") from None
    bind_source_versions(value, {})
    if not isinstance(value["evidence_needed"], list) or len(value["evidence_needed"]) > 32 or any(
            not identifier(item) for item in value["evidence_needed"]):
        raise RecoveryError("invalid_evidence_needed")
    if value["state"] in {"unknown", "held"} and (not value["reason_code"] or not value["evidence_needed"]):
        raise RecoveryError("unexplained_hold")
    receipt = value["receipt_sha256"]
    if receipt is not None and (not isinstance(receipt, str) or not HASH.fullmatch(receipt)):
        raise RecoveryError("invalid_receipt_digest")
    if value["state"] in {"confirmed", "failed"} and (value["kind"] != "receipt" or not receipt):
        raise RecoveryError("outcome_requires_receipt")
    expected_state = {"definition": "defined", "request": "requested", "attempt": "attempted", "hold": "held"}
    if value["kind"] in expected_state and value["state"] != expected_state[value["kind"]]:
        raise RecoveryError("kind_state_conflict")
    if value["kind"] == "correction" and not value["supersedes_event_id"]:
        raise RecoveryError("correction_requires_original")
    context = value.get("source_context")
    if context is not None:
        if not isinstance(context, dict) or set(context) != {"schema_version", "contract_ref", "record_ref", "alias_ref", "holds_ref", "unknown_boundary"} or context["schema_version"] != "2.0.0":
            raise RecoveryError("invalid_source_context")
        for name in ("contract_ref", "record_ref", "alias_ref", "holds_ref"):
            ref = context[name]
            if (not isinstance(ref, dict) or set(ref) != {"sha256", "json_pointer"}
                    or not isinstance(ref["sha256"], str) or not HASH.fullmatch(ref["sha256"])
                    or not isinstance(ref["json_pointer"], str)
                    or not re.fullmatch(r"(?:/(?:[A-Za-z0-9_-]|~[01]){1,80}){0,8}", ref["json_pointer"])):
                raise RecoveryError("invalid_original_reference")
        if type(context["unknown_boundary"]) is not bool:
            raise RecoveryError("invalid_boundary_status")
        if context["unknown_boundary"] and value["state"] not in {"unknown", "held"}:
            raise RecoveryError("unknown_boundary_requires_hold")
    # Only structured opaque references are accepted; no URLs, bodies or saved permissions.
    if len(canonical(value).encode()) > 12000:
        raise RecoveryError("metadata_too_large")
    return value


class RecoveryCache:
    """One cache per workspace/history/source scope; SQLite transactions checkpoint replay.

    identity must come from a fresh authenticated workspace read and an independently
    pinned source manifest. A history ID is an operator restore epoch: the hosted API
    currently has no history-epoch field. Never reuse it across an older restore.
    """

    def __init__(self, path, *, workspace_id, history_id, source_manifest_sha256):
        if not identifier(workspace_id) or not identifier(history_id) or not HASH.fullmatch(source_manifest_sha256):
            raise RecoveryError("ambiguous_identity")
        self.identity = {"workspace_id": workspace_id, "history_id": history_id,
                         "source_manifest_sha256": source_manifest_sha256}
        self.db = sqlite3.connect(Path(path), timeout=10, isolation_level=None)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                self.db.execute("CREATE TABLE meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER, identity TEXT, high_watermark INTEGER NOT NULL)")
                self.db.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, metadata_sha256 TEXT, chain TEXT NOT NULL, record_key TEXT NOT NULL, revision INTEGER NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL, UNIQUE(record_key,revision), UNIQUE(actor,request_id))")
                self.db.execute("CREATE TABLE metadata (event_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, record_version INTEGER NOT NULL, value TEXT NOT NULL, fingerprint TEXT NOT NULL, sequence INTEGER NOT NULL UNIQUE, UNIQUE(workflow_id,record_version))")
                self.db.execute("INSERT INTO meta VALUES(1,?,?,0)", (SCHEMA_VERSION, canonical(self.identity)))
            row = self.db.execute("SELECT version,identity FROM meta WHERE singleton=1").fetchone()
            if row != (SCHEMA_VERSION, canonical(self.identity)):
                raise RecoveryError("cache_schema_or_identity_mismatch")
            self.db.execute("COMMIT")
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def checkpoint(self):
        owned = not self.db.in_transaction
        if owned:
            self.db.execute("BEGIN")
        try:
            row = self.db.execute("SELECT sequence,chain FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
            return {**self.identity, "schema_version": SCHEMA_VERSION,
                    "last_durable_sequence": row[0] if row else 0,
                    "last_durable_sha256": row[1] if row else digest(self.identity),
                    "source_high_watermark": self.db.execute("SELECT high_watermark FROM meta WHERE singleton=1").fetchone()[0]}
        finally:
            if owned:
                self.db.execute("ROLLBACK")

    def apply_page(self, page, *, workspace_id, history_id, source_manifest_sha256):
        """Atomically apply a complete ordered page, or hold with no cursor movement.

        Unknown non-workflow records are hashed, never retained. Every orgwf: record
        must use the strict metadata schema. Feed only pages from the pinned tenant.
        """
        if {"workspace_id": workspace_id, "history_id": history_id,
                "source_manifest_sha256": source_manifest_sha256} != self.identity:
            raise RecoveryError("tenant_history_or_scope_mismatch")
        if (not isinstance(page, dict) or not {"events", "next_cursor", "high_watermark", "has_more"} <= set(page)
                or not isinstance(page["events"], list) or len(page["events"]) > 100
                or not integer(page["next_cursor"]) or not integer(page["high_watermark"])
                or type(page["has_more"]) is not bool):
            raise RecoveryError("invalid_page")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cp = self.checkpoint()
            seq, chain = cp["last_durable_sequence"], cp["last_durable_sha256"]
            if page["high_watermark"] < max(seq, cp["source_high_watermark"]):
                raise RecoveryError("source_regressed_or_restored")
            previous = 0
            canonical_versions = {}
            source_versions = None
            for event in page["events"]:
                if (not isinstance(event, dict) or not integer(event.get("sequence"), 1)
                        or event["sequence"] <= previous or event["sequence"] > page["high_watermark"]
                        or not identifier(event.get("actor")) or not identifier(event.get("request_id"))):
                    raise RecoveryError("invalid_event_order_or_identity")
                previous = event["sequence"]
                record = event.get("record")
                if (not isinstance(record, dict) or not {"key", "revision", "value"} <= set(record)
                        or not identifier(record["key"]) or not integer(record["revision"], 1)):
                    raise RecoveryError("invalid_record")
                fingerprint = digest(event)
                if event["sequence"] <= seq:
                    existing = self.db.execute("SELECT fingerprint FROM events WHERE sequence=?", (event["sequence"],)).fetchone()
                    if existing != (fingerprint,):
                        raise RecoveryError("conflicting_replay")
                    continue
                if event["sequence"] != seq + 1:
                    raise RecoveryError("missing_events")
                revision = self.db.execute("SELECT max(revision) FROM events WHERE record_key=?", (record["key"],)).fetchone()[0] or 0
                if record["revision"] != revision + 1:
                    raise RecoveryError("missing_or_conflicting_record_version")
                metadata_hash = None
                if record["key"].startswith("orgwf:"):
                    value = validate_metadata(record["value"])
                    metadata_hash = digest(value)
                    if record["key"] != "orgwf:" + value["event_id"] or record["revision"] != 1:
                        raise RecoveryError("workflow_events_are_immutable")
                    if value["source_manifest_sha256"] != self.identity["source_manifest_sha256"]:
                        raise RecoveryError("source_scope_mismatch")
                    if source_versions is None:
                        source_versions = {}
                        for (prior_raw,) in self.db.execute("SELECT value FROM metadata"):
                            bind_source_versions(validate_metadata(json.loads(prior_raw)), source_versions)
                    bind_source_versions(value, source_versions)
                    workflow = value["workflow_id"]
                    if workflow not in canonical_versions:
                        versions = canonical_versions[workflow] = {}
                        for (existing_raw,) in self.db.execute("SELECT value FROM metadata WHERE workflow_id=?", (workflow,)):
                            existing_value = json.loads(existing_raw)
                            seen = versions.setdefault(existing_value["record_version"], existing_value["content_sha256"])
                            if seen != existing_value["content_sha256"]:
                                raise RecoveryError("canonical_version_conflict")
                    seen = canonical_versions[workflow].setdefault(value["record_version"], value["content_sha256"])
                    if seen != value["content_sha256"]:
                        raise RecoveryError("canonical_version_conflict")
                    last = self.db.execute("SELECT max(record_version) FROM metadata WHERE workflow_id=?", (value["workflow_id"],)).fetchone()[0] or 0
                    if value["workflow_event_version"] != last + 1:
                        raise RecoveryError("workflow_version_gap_or_conflict")
                    if value["supersedes_event_id"]:
                        original = self.db.execute("SELECT workflow_id FROM metadata WHERE event_id=?", (value["supersedes_event_id"],)).fetchone()
                        if original != (value["workflow_id"],):
                            raise RecoveryError("missing_or_foreign_original")
                    if value["kind"] == "receipt":
                        previous_occurrence = None
                        for (raw,) in self.db.execute("SELECT value FROM metadata WHERE workflow_id=? ORDER BY record_version DESC", (value["workflow_id"],)):
                            prior = json.loads(raw)
                            if prior["occurrence_id"] == value["occurrence_id"] and prior["kind"] in {"request", "attempt", "receipt", "hold"}:
                                previous_occurrence = prior
                                break
                        if not previous_occurrence or previous_occurrence["kind"] not in {"attempt", "hold"}:
                            raise RecoveryError("receipt_requires_attempt_or_reconciliation_hold")
                    self.db.execute("INSERT INTO metadata VALUES(?,?,?,?,?,?)", (value["event_id"], value["workflow_id"], value["workflow_event_version"], canonical(value), digest(value), event["sequence"]))
                seq = event["sequence"]
                chain = digest({"previous": chain, "sequence": seq, "event_sha256": fingerprint,
                                "metadata_sha256": metadata_hash})
                self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)", (seq, fingerprint, metadata_hash, chain, record["key"], record["revision"], event["actor"], event["request_id"]))
            if page["next_cursor"] != seq or page["has_more"] != (seq < page["high_watermark"]):
                raise RecoveryError("cursor_or_completeness_mismatch")
            self.db.execute("UPDATE meta SET high_watermark=? WHERE singleton=1", (page["high_watermark"],))
            self.db.execute("COMMIT")
            return self.checkpoint()
        except Exception as error:
            self.db.execute("ROLLBACK")
            if isinstance(error, sqlite3.IntegrityError):
                raise RecoveryError("duplicate_identity_conflict") from None
            raise

    def recover(self, workflow_id, *, checkpoint, current_workspace_id=None,
                current_scope=None, current_source_manifest_sha256=None):
        """Return bounded historical context only. Fresh authorization is never persisted.

        checkpoint must be independently retained outside this cache. Even complete
        recovery cannot grant permission or schedule an action.
        """
        self.db.execute("BEGIN")
        try:
            return self._recover(workflow_id, checkpoint, current_workspace_id,
                                 current_scope, current_source_manifest_sha256)
        finally:
            self.db.execute("ROLLBACK")

    def _recover(self, workflow_id, checkpoint, current_workspace_id, current_scope,
                 current_source_manifest_sha256):
        if checkpoint != self.checkpoint():
            raise RecoveryError("stale_missing_or_restored_checkpoint")
        if (current_workspace_id != self.identity["workspace_id"] or current_scope not in {"read", "write", "owner"}
                or current_source_manifest_sha256 != self.identity["source_manifest_sha256"]):
            raise RecoveryError("fresh_access_and_source_scope_required")
        if not identifier(workflow_id):
            raise RecoveryError("invalid_workflow_id")
        chain, sequence = digest(self.identity), 0
        for seq, fingerprint, metadata_hash, stored_chain in self.db.execute("SELECT sequence,fingerprint,metadata_sha256,chain FROM events ORDER BY sequence"):
            sequence += 1
            chain = digest({"previous": chain, "sequence": seq, "event_sha256": fingerprint,
                            "metadata_sha256": metadata_hash})
            if seq != sequence or chain != stored_chain:
                raise RecoveryError("cache_integrity_failure")
            if metadata_hash is not None:
                original = self.db.execute("SELECT value FROM metadata WHERE sequence=?", (seq,)).fetchone()
                if not original or digest(json.loads(original[0])) != metadata_hash:
                    raise RecoveryError("cache_integrity_failure")
        canonical_versions, source_versions = {}, {}
        for raw, fingerprint in self.db.execute("SELECT value,fingerprint FROM metadata"):
            audited = validate_metadata(json.loads(raw))
            if digest(audited) != fingerprint:
                raise RecoveryError("cache_integrity_failure")
            bind_source_versions(audited, source_versions)
            identity = (audited["workflow_id"], audited["record_version"])
            previous_digest = canonical_versions.setdefault(identity, audited["content_sha256"])
            if previous_digest != audited["content_sha256"]:
                raise RecoveryError("canonical_version_conflict")
        rows = self.db.execute("SELECT value FROM metadata WHERE workflow_id=? ORDER BY record_version DESC LIMIT 33", (workflow_id,)).fetchall()
        values = [json.loads(r[0]) for r in rows[:32]]
        latest = values[0] if values else None
        # Executions and source/identity holds are independent tracks. Metadata
        # corrections do not manufacture executions; receipts cannot clear a hold.
        occurrences, holds = {}, {}
        for (raw,) in self.db.execute("SELECT value FROM metadata WHERE workflow_id=? ORDER BY record_version", (workflow_id,)):
            value = json.loads(raw)
            if value["kind"] in {"request", "attempt", "receipt"}:
                occurrences[value["occurrence_id"]] = value
            if value["kind"] == "hold" or value["state"] in {"unknown", "held"}:
                holds[value["event_id"]] = value
            original_hold = holds.get(value["supersedes_event_id"])
            if original_hold and value["kind"] == "correction" and value["state"] == "defined":
                boundary_hold = original_hold.get("source_context", {}).get("unknown_boundary", False)
                reason = "boundary_resolved" if boundary_hold else "hold_resolved"
                context = value.get("source_context")
                original_context = original_hold.get("source_context")
                references_retained = (original_context is None or
                    (context is not None and all(context[name] == original_context[name]
                     for name in ("contract_ref", "record_ref", "alias_ref", "holds_ref"))))
                # A resolution may add evidence but must not substitute or erase the
                # evidence identifying the held record. Order is not source identity.
                sources_retained = all(source in value["sources"] for source in original_hold["sources"])
                explicit_resolution = (references_retained and sources_retained
                                       and value["reason_code"] == reason and not value["evidence_needed"]
                                       and (not boundary_hold or (context is not None and not context["unknown_boundary"])))
                if explicit_resolution:
                    del holds[value["supersedes_event_id"]]
        unresolved = [{"occurrence_id": oid, "state": v["state"], "event_id": v["event_id"]}
                      for oid, v in occurrences.items() if v["state"] not in {"confirmed", "failed"}]
        hold_details = [{"event_id": v["event_id"], "occurrence_id": v["occurrence_id"],
                         "reason_code": v["reason_code"],
                         "unknown_boundary": v.get("source_context", {}).get("unknown_boundary", False)}
                        for v in holds.values()]
        truncated = len(rows) > 32 or len(unresolved) > 32 or len(hold_details) > 32
        return {"workflow_id": workflow_id, "records": values, "truncated": truncated,
                "status": "held" if (not latest or unresolved or holds or truncated
                                      or latest["department"] == "unknown"
                                      or checkpoint["last_durable_sequence"] < checkpoint["source_high_watermark"]) else "historical_context",
                "unresolved_occurrences": unresolved[:32], "unresolved_occurrence_count": len(unresolved),
                "unresolved_occurrences_truncated": len(unresolved) > 32,
                "unresolved_holds": hold_details[:32], "unresolved_hold_count": len(hold_details),
                "unresolved_holds_truncated": len(hold_details) > 32, "action_authorized": False,
                "checkpoint": self.checkpoint()}

    def backup(self, destination):
        """Consistent SQLite snapshot to a new path; never overwrite existing files."""
        path = Path(destination)
        with path.open("xb"):
            pass
        target = sqlite3.connect(path)
        try:
            self.db.backup(target)
            row = target.execute("SELECT sequence,chain FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
            return {**self.identity, "schema_version": SCHEMA_VERSION,
                    "last_durable_sequence": row[0] if row else 0,
                    "last_durable_sha256": row[1] if row else digest(self.identity),
                    "source_high_watermark": target.execute("SELECT high_watermark FROM meta WHERE singleton=1").fetchone()[0]}
        finally:
            target.close()
