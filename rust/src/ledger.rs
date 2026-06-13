//! Idempotency ledger for guarding money/state operations against double
//! execution on retry.
//!
//! Generalized from the JSONL audit-ledger replay guard in `swarmfi-executor`
//! (`sfe-core/src/audit.rs` + `sfe-executor` `execute_signal`) and the matching
//! "idempotent: transfer already completed" guard in `cleanmandate`
//! (`cm-executor`). Both pin the same P0 fix: a retrying caller (cron wrapper,
//! at-least-once queue) must never re-dispatch the same logical operation.
//!
//! The contract is intentionally tiny: `contains(key)` to check before doing
//! the work, `record(key)` to mark it done after. Keys are caller-chosen stable
//! identifiers (a signal UUID, a mandate id, a payment idempotency key, ...).

use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use thiserror::Error;

/// Errors from ledger operations.
#[derive(Debug, Error)]
pub enum LedgerError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("ledger lock poisoned")]
    Poisoned,
}

/// Guards money/state operations against duplicate execution.
///
/// Typical use:
/// ```ignore
/// if ledger.contains(&key)? {
///     return Ok(/* idempotent replay: skip side effect */);
/// }
/// do_the_money_thing()?;
/// ledger.record(&key)?;
/// ```
pub trait IdempotencyLedger {
    /// Returns `true` if `key` has already been recorded (the op already ran).
    fn contains(&self, key: &str) -> Result<bool, LedgerError>;

    /// Records `key` as executed. Idempotent: recording an already-present key
    /// is a no-op success.
    fn record(&self, key: &str) -> Result<(), LedgerError>;
}

/// One line of the JSONL ledger file.
#[derive(Debug, Serialize, Deserialize)]
struct LedgerEntry {
    key: String,
}

/// File/JSONL-backed [`IdempotencyLedger`].
///
/// Append-only: each recorded key is one JSON line. An in-memory `HashSet`
/// mirror (loaded on open, kept in sync on `record`) keeps `contains` O(1) and
/// avoids re-reading the file. The append-only layout mirrors the swarmfi audit
/// ledger and is crash-safe in the sense that a partially written final line is
/// simply ignored on reload.
pub struct FileLedger {
    path: PathBuf,
    seen: Mutex<HashSet<String>>,
}

impl FileLedger {
    /// Open (or create) a ledger at `path`, loading any existing keys.
    pub fn open(path: impl Into<PathBuf>) -> Result<Self, LedgerError> {
        let path = path.into();
        let seen = load_keys(&path)?;
        Ok(Self {
            path,
            seen: Mutex::new(seen),
        })
    }

    /// Number of distinct keys currently recorded.
    pub fn len(&self) -> Result<usize, LedgerError> {
        Ok(self.seen.lock().map_err(|_| LedgerError::Poisoned)?.len())
    }

    /// Whether the ledger has no recorded keys.
    pub fn is_empty(&self) -> Result<bool, LedgerError> {
        Ok(self.len()? == 0)
    }
}

fn load_keys(path: &Path) -> Result<HashSet<String>, LedgerError> {
    let mut set = HashSet::new();
    if !path.exists() {
        return Ok(set);
    }
    let reader = BufReader::new(File::open(path)?);
    for line in reader.lines() {
        let line = line?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        // Tolerate a torn final line from a crash mid-append: skip unparseable
        // tails rather than failing the whole load.
        if let Ok(entry) = serde_json::from_str::<LedgerEntry>(trimmed) {
            set.insert(entry.key);
        }
    }
    Ok(set)
}

impl IdempotencyLedger for FileLedger {
    fn contains(&self, key: &str) -> Result<bool, LedgerError> {
        Ok(self
            .seen
            .lock()
            .map_err(|_| LedgerError::Poisoned)?
            .contains(key))
    }

    fn record(&self, key: &str) -> Result<(), LedgerError> {
        let mut guard = self.seen.lock().map_err(|_| LedgerError::Poisoned)?;
        if guard.contains(key) {
            return Ok(());
        }
        if let Some(parent) = self.path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        let entry = LedgerEntry {
            key: key.to_string(),
        };
        writeln!(file, "{}", serde_json::to_string(&entry)?)?;
        file.flush()?;
        guard.insert(key.to_string());
        Ok(())
    }
}
