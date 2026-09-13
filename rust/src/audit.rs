//! Signed, append-only JSONL audit ledger.
//!
//! Lifted and generalized from the HMAC-SHA256 audit ledgers in `cleanmandate`,
//! `swarmfi-executor`, `glacier-edge-arm`, and `compliance-as-code-agent`
//! (`*-core/src/audit.rs`), which each write one JSON line per decision/event
//! with a `content_hash` and an HMAC signature. This generalizes that scheme and
//! adds **signature chaining**: every record signs the *previous* record's
//! signature, so the ledger is tamper-evident and truly append-only — an
//! interior line cannot be edited, reordered, or deleted without breaking every
//! signature that follows it.
//!
//! Signing scheme (identical across the TS / Python / Rust ports):
//!
//! ```text
//! canonical = canonical_json({ts, event, actor, inputs, sources,
//!                             confidence?, rationale?, prev_sig})
//! sig       = hex( HMAC-SHA256(key, canonical) )
//! ```
//!
//! Canonical JSON emits object keys in sorted order with no insignificant
//! whitespace (via `serde_json::Value`'s `BTreeMap` ordering), so the signed
//! bytes are stable regardless of insertion order. The genesis record uses
//! `prev_sig = ""`.

use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::Sha256;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use thiserror::Error;

type HmacSha256 = Hmac<Sha256>;

/// The environment variable read for the signing key.
pub const AUDIT_LEDGER_KEY_ENV: &str = "AUDIT_LEDGER_KEY";

/// The default signing key. Documented and safe ONLY for tests/dev.
pub const DEFAULT_AUDIT_LEDGER_KEY: &str = "cubiczan-resilience-insecure-default-key";

/// Errors from audit-ledger operations.
#[derive(Debug, Error)]
pub enum AuditError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("audit ledger lock poisoned")]
    Poisoned,
    #[error("audit ledger path escapes allowed base directory")]
    PathEscape,
}

/// Caller-supplied fields of an audit record. `inputs` and `sources` accept any
/// JSON value; optional fields default to omitted.
#[derive(Debug, Clone, Default)]
pub struct AuditRecordInput {
    /// What happened (a decision / event name).
    pub event: String,
    /// Who/what performed it (agent, user, service).
    pub actor: String,
    /// The inputs the decision was made from.
    pub inputs: Option<Value>,
    /// Provenance: where the inputs/evidence came from.
    pub sources: Option<Value>,
    /// Optional confidence score for the decision.
    pub confidence: Option<f64>,
    /// Optional human-readable rationale.
    pub rationale: Option<String>,
    /// RFC 3339 timestamp. Defaults to the current UTC time. Supply this to make
    /// records deterministic in tests.
    pub ts: Option<String>,
}

/// A fully materialized, signed ledger record (one JSONL line).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuditRecord {
    pub ts: String,
    pub event: String,
    pub actor: String,
    pub inputs: Value,
    pub sources: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rationale: Option<String>,
    /// Signature of the prior record (`""` for the genesis record).
    pub prev_sig: String,
    /// HMAC-SHA256 over the canonical record including `prev_sig`.
    pub sig: String,
}

/// Outcome of [`AuditLedger::verify`] / [`verify_ledger`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VerifyResult {
    /// The whole chain re-derives correctly. Carries the number of records.
    Ok { count: usize },
    /// Verification failed at `tampered_index` (zero-based) with `reason`.
    Tampered { tampered_index: usize, reason: String },
}

impl VerifyResult {
    /// Whether the ledger verified intact.
    pub fn is_ok(&self) -> bool {
        matches!(self, VerifyResult::Ok { .. })
    }

    /// The index of the first tampered line, if any.
    pub fn tampered_index(&self) -> Option<usize> {
        match self {
            VerifyResult::Tampered { tampered_index, .. } => Some(*tampered_index),
            VerifyResult::Ok { .. } => None,
        }
    }
}

/// File-backed, HMAC-signed, append-only JSONL audit ledger with per-record
/// signature chaining.
///
/// ```ignore
/// let ledger = AuditLedger::open(".state/audit.jsonl", None)?;
/// let sig = ledger.append(AuditRecordInput {
///     event: "approve_payout".into(),
///     actor: "cfo-agent".into(),
///     inputs: Some(serde_json::json!({ "amount": 1000 })),
///     ..Default::default()
/// })?;
/// assert!(ledger.verify()?.is_ok());
/// ```
pub struct AuditLedger {
    path: PathBuf,
    base_dir: PathBuf,
    key: String,
    /// Signature of the last appended record; seeds the next `prev_sig`.
    last_sig: Mutex<String>,
}

impl AuditLedger {
    /// Open (or create) a ledger at `path`. When `key` is `None`, the signing
    /// key is resolved from the `AUDIT_LEDGER_KEY` env var, then
    /// [`DEFAULT_AUDIT_LEDGER_KEY`]. The chain resumes from any existing file.
    ///
    /// `path` is resolved and must stay under the process cwd. Use
    /// [`AuditLedger::open_under`] to confine to a different base directory.
    pub fn open(path: impl Into<PathBuf>, key: Option<String>) -> Result<Self, AuditError> {
        let cwd = std::env::current_dir()?;
        Self::open_under(path, key, cwd)
    }

    /// Open a ledger at `path`, rejecting any path that escapes `base_dir`.
    pub fn open_under(
        path: impl Into<PathBuf>,
        key: Option<String>,
        base_dir: impl AsRef<Path>,
    ) -> Result<Self, AuditError> {
        let base_dir = resolve_base_dir(base_dir.as_ref())?;
        let path = confine_path(&path.into(), &base_dir)?;
        let last_sig = read_records(&path)?
            .last()
            .map(|r| r.sig.clone())
            .unwrap_or_default();
        Ok(Self {
            path,
            base_dir,
            key: resolve_key(key),
            last_sig: Mutex::new(last_sig),
        })
    }

    /// Append one record, chained to the prior signature, and return its `sig`.
    pub fn append(&self, input: AuditRecordInput) -> Result<String, AuditError> {
        let mut last = self.last_sig.lock().map_err(|_| AuditError::Poisoned)?;

        let mut record = AuditRecord {
            ts: input.ts.unwrap_or_else(now_rfc3339),
            event: input.event,
            actor: input.actor,
            inputs: input.inputs.unwrap_or(Value::Null),
            sources: input.sources.unwrap_or(Value::Null),
            confidence: input.confidence,
            rationale: input.rationale,
            prev_sig: last.clone(),
            sig: String::new(),
        };
        record.sig = sign(&self.key, &record);

        if let Some(parent) = self.path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        writeln!(file, "{}", serde_json::to_string(&record)?)?;
        file.flush()?;

        *last = record.sig.clone();
        Ok(record.sig)
    }

    /// Re-walk the ledger and recompute every signature in-chain.
    pub fn verify(&self) -> Result<VerifyResult, AuditError> {
        verify_ledger_under(&self.path, Some(&self.key), &self.base_dir)
    }
}

/// Verify a ledger file without constructing an [`AuditLedger`]. Re-derives each
/// signature from the stored content plus the running `prev_sig` and returns the
/// index of the first broken line. When `key` is `None`, resolves it the same
/// way [`AuditLedger::open`] does.
///
/// `path` is resolved and must stay under the process cwd. Use
/// [`verify_ledger_under`] to confine to a different base directory.
pub fn verify_ledger(path: &Path, key: Option<&str>) -> Result<VerifyResult, AuditError> {
    let cwd = std::env::current_dir()?;
    verify_ledger_under(path, key, &cwd)
}

/// Verify a ledger file, resolving `path` and rejecting anything that escapes
/// `base_dir`.
pub fn verify_ledger_under(
    path: &Path,
    key: Option<&str>,
    base_dir: &Path,
) -> Result<VerifyResult, AuditError> {
    let path = confine_path(path, base_dir)?;
    let resolved = key.map(str::to_string).unwrap_or_else(|| resolve_key(None));
    let records = read_records(&path)?;

    let mut prev_sig = String::new();
    for (i, record) in records.iter().enumerate() {
        if record.prev_sig != prev_sig {
            return Ok(VerifyResult::Tampered {
                tampered_index: i,
                reason: format!("prev_sig mismatch: chain broken at line {i}"),
            });
        }
        let expected = sign(&resolved, record);
        if expected != record.sig {
            return Ok(VerifyResult::Tampered {
                tampered_index: i,
                reason: format!("signature mismatch at line {i}"),
            });
        }
        prev_sig = record.sig.clone();
    }
    Ok(VerifyResult::Ok {
        count: records.len(),
    })
}

fn resolve_key(key: Option<String>) -> String {
    key.or_else(|| std::env::var(AUDIT_LEDGER_KEY_ENV).ok())
        .unwrap_or_else(|| DEFAULT_AUDIT_LEDGER_KEY.to_string())
}

fn resolve_base_dir(base_dir: &Path) -> Result<PathBuf, AuditError> {
    if base_dir.is_absolute() {
        return Ok(normalize_lexically(base_dir));
    }
    Ok(normalize_lexically(&std::env::current_dir()?.join(base_dir)))
}

/// Resolve `user_path` against `base_dir` and reject it if the result escapes
/// that directory (relative `..` traversal or an absolute path outside the
/// base). Uses lexical normalization so the ledger file need not exist yet.
fn confine_path(user_path: &Path, base_dir: &Path) -> Result<PathBuf, AuditError> {
    let base = resolve_base_dir(base_dir)?;
    let resolved = if user_path.is_absolute() {
        normalize_lexically(user_path)
    } else {
        normalize_lexically(&base.join(user_path))
    };
    if !resolved.starts_with(&base) {
        return Err(AuditError::PathEscape);
    }
    Ok(resolved)
}

fn normalize_lexically(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::Prefix(_) | Component::RootDir => out.push(comp.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(c) => out.push(c),
        }
    }
    out
}

/// The canonical bytes signed for a record: content + `prev_sig`, never `sig`.
/// Optional fields are included only when present, so the canonical form matches
/// on both append and verify. `serde_json::Value`'s map is a `BTreeMap`, so
/// serialization is key-sorted and whitespace-free — a stable canonical form.
fn signing_payload(record: &AuditRecord) -> Value {
    let mut map = serde_json::Map::new();
    map.insert("ts".into(), Value::String(record.ts.clone()));
    map.insert("event".into(), Value::String(record.event.clone()));
    map.insert("actor".into(), Value::String(record.actor.clone()));
    map.insert("inputs".into(), record.inputs.clone());
    map.insert("sources".into(), record.sources.clone());
    if let Some(c) = record.confidence {
        if let Some(n) = serde_json::Number::from_f64(c) {
            map.insert("confidence".into(), Value::Number(n));
        }
    }
    if let Some(r) = &record.rationale {
        map.insert("rationale".into(), Value::String(r.clone()));
    }
    map.insert("prev_sig".into(), Value::String(record.prev_sig.clone()));
    Value::Object(map)
}

fn sign(key: &str, record: &AuditRecord) -> String {
    let canonical = serde_json::to_vec(&signing_payload(record)).unwrap_or_default();
    let mut mac = HmacSha256::new_from_slice(key.as_bytes()).expect("HMAC accepts any key length");
    mac.update(&canonical);
    hex::encode(mac.finalize().into_bytes())
}

fn read_records(path: &Path) -> Result<Vec<AuditRecord>, AuditError> {
    if !path.exists() {
        return Ok(Vec::new());
    }
    let reader = BufReader::new(File::open(path)?);
    let mut records = Vec::new();
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        records.push(serde_json::from_str(&line)?);
    }
    Ok(records)
}

/// Minimal RFC 3339 / ISO-8601 UTC timestamp from `SystemTime`, avoiding a
/// `chrono` dependency. Format: `YYYY-MM-DDThh:mm:ssZ` (whole seconds).
fn now_rfc3339() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format_epoch_utc(secs)
}

fn format_epoch_utc(secs: u64) -> String {
    // Days since epoch and time-of-day.
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (hh, mm, ss) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    let (y, mo, d) = civil_from_days(days);
    format!("{y:04}-{mo:02}-{d:02}T{hh:02}:{mm:02}:{ss:02}Z")
}

/// Howard Hinnant's `civil_from_days`: convert days-since-epoch to (y, m, d).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}
