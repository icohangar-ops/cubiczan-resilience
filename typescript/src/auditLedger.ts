import { createHmac } from "node:crypto";
import {
  appendFileSync,
  mkdirSync,
  readFileSync,
  existsSync,
} from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";

/**
 * Signed, append-only JSONL audit ledger.
 *
 * Lifted and generalized from the HMAC-SHA256 audit ledgers in `cleanmandate`,
 * `swarmfi-executor`, `glacier-edge-arm`, and `compliance-as-code-agent`
 * (`*-core/src/audit.rs`), which each write one JSON line per decision/event
 * with a `content_hash` and an HMAC signature. This generalizes that scheme and
 * adds **signature chaining**: every record signs the *previous* record's
 * signature, so the ledger is tamper-evident and truly append-only — you cannot
 * edit, reorder, or delete an interior line without breaking every signature
 * that follows it.
 *
 * Signing scheme (identical across the TS / Python / Rust ports):
 *
 *   canonical = canonicalJson({ ts, event, actor, inputs, sources,
 *                               confidence?, rationale?, prev_sig })
 *   sig       = hex( HMAC-SHA256(key, canonical) )
 *
 * `canonicalJson` emits object keys in sorted order with no insignificant
 * whitespace, so the signed bytes are stable regardless of insertion order.
 * The genesis record uses `prev_sig = ""`.
 */

/** The default signing key. Documented and safe ONLY for tests/dev. */
export const DEFAULT_AUDIT_LEDGER_KEY =
  "cubiczan-resilience-insecure-default-key";

/** The environment variable read for the signing key. */
export const AUDIT_LEDGER_KEY_ENV = "AUDIT_LEDGER_KEY";

/** Caller-supplied fields of an audit record. */
export interface AuditRecordInput {
  /** What happened (a decision / event name). */
  readonly event: string;
  /** Who/what performed it (agent, user, service). */
  readonly actor: string;
  /** The inputs the decision was made from. */
  readonly inputs?: unknown;
  /** Provenance: where the inputs/evidence came from. */
  readonly sources?: unknown;
  /** Optional confidence score for the decision. */
  readonly confidence?: number;
  /** Optional human-readable rationale. */
  readonly rationale?: string;
  /**
   * RFC 3339 timestamp. Defaults to `new Date().toISOString()`. Supply this to
   * make records deterministic in tests.
   */
  readonly ts?: string;
}

/** A fully materialized, signed ledger record (one JSONL line). */
export interface AuditRecord {
  readonly ts: string;
  readonly event: string;
  readonly actor: string;
  readonly inputs: unknown;
  readonly sources: unknown;
  readonly confidence?: number;
  readonly rationale?: string;
  /** Signature of the prior record (`""` for the genesis record). */
  readonly prev_sig: string;
  /** HMAC-SHA256 over the canonical record including `prev_sig`. */
  readonly sig: string;
}

/** Result of {@link AuditLedger.verify}. */
export type VerifyResult =
  | { readonly ok: true; readonly count: number }
  | {
      readonly ok: false;
      /** Zero-based index of the first line that fails verification. */
      readonly tamperedIndex: number;
      readonly reason: string;
    };

export interface AuditLedgerOptions {
  /** Path to the JSONL ledger file. */
  readonly path: string;
  /**
   * HMAC key. Defaults to `process.env.AUDIT_LEDGER_KEY`, then to
   * {@link DEFAULT_AUDIT_LEDGER_KEY} (test-only).
   */
  readonly key?: string;
  /**
   * Directory that {@link AuditLedgerOptions.path} must resolve under.
   * Defaults to `process.cwd()`. Paths that escape this directory (e.g. via
   * `..`) are rejected.
   */
  readonly baseDir?: string;
}

/** Stable, whitespace-free JSON with recursively sorted object keys. */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortKeys(value));
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(value as Record<string, unknown>).sort()) {
      out[k] = sortKeys((value as Record<string, unknown>)[k]);
    }
    return out;
  }
  return value;
}

function resolveKey(key?: string): string {
  return key ?? process.env[AUDIT_LEDGER_KEY_ENV] ?? DEFAULT_AUDIT_LEDGER_KEY;
}

/**
 * Resolve `userPath` against `baseDir` and reject it if the result escapes
 * that directory (relative `..` traversal or an absolute path outside the
 * base).
 */
function confinePath(userPath: string, baseDir: string): string {
  const resolvedBase = resolve(baseDir);
  const resolved = resolve(resolvedBase, userPath);
  const rel = relative(resolvedBase, resolved);
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error(
      `audit ledger path escapes allowed base directory: ${userPath}`,
    );
  }
  return resolved;
}

/**
 * The exact bytes that get signed for a record. Only the content fields plus
 * `prev_sig` are covered — never `sig` itself. Optional fields are omitted when
 * absent so the canonical form matches on both append and verify.
 */
function signingPayload(
  rec: Omit<AuditRecord, "sig">,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    ts: rec.ts,
    event: rec.event,
    actor: rec.actor,
    inputs: rec.inputs,
    sources: rec.sources,
    prev_sig: rec.prev_sig,
  };
  if (rec.confidence !== undefined) payload.confidence = rec.confidence;
  if (rec.rationale !== undefined) payload.rationale = rec.rationale;
  return payload;
}

function sign(key: string, rec: Omit<AuditRecord, "sig">): string {
  return createHmac("sha256", key)
    .update(canonicalJson(signingPayload(rec)))
    .digest("hex");
}

function parseLines(raw: string): AuditRecord[] {
  const records: AuditRecord[] = [];
  for (const line of raw.split("\n")) {
    if (line.trim() === "") continue;
    records.push(JSON.parse(line) as AuditRecord);
  }
  return records;
}

/**
 * File-backed, HMAC-signed, append-only JSONL audit ledger with per-record
 * signature chaining.
 */
export class AuditLedger {
  private readonly path: string;
  private readonly key: string;
  private readonly baseDir: string;
  /** Signature of the last appended record; seeds the next `prev_sig`. */
  private lastSig: string;

  constructor(options: AuditLedgerOptions) {
    this.baseDir = resolve(options.baseDir ?? process.cwd());
    this.path = confinePath(options.path, this.baseDir);
    this.key = resolveKey(options.key);
    // Resume the chain from an existing file so appends stay linked.
    this.lastSig = existsSync(this.path)
      ? parseLines(readFileSync(this.path, "utf-8")).at(-1)?.sig ?? ""
      : "";
  }

  /**
   * Append one record, chaining it to the previous line's signature, and
   * return the new record's signature.
   */
  append(input: AuditRecordInput): string {
    const unsigned: Omit<AuditRecord, "sig"> = {
      ts: input.ts ?? new Date().toISOString(),
      event: input.event,
      actor: input.actor,
      inputs: input.inputs ?? null,
      sources: input.sources ?? null,
      ...(input.confidence !== undefined
        ? { confidence: input.confidence }
        : {}),
      ...(input.rationale !== undefined ? { rationale: input.rationale } : {}),
      prev_sig: this.lastSig,
    };
    const sig = sign(this.key, unsigned);
    const record: AuditRecord = { ...unsigned, sig };

    const dir = dirname(this.path);
    if (dir && dir !== ".") mkdirSync(dir, { recursive: true });
    appendFileSync(this.path, JSON.stringify(record) + "\n");

    this.lastSig = sig;
    return sig;
  }

  /**
   * Re-walk the whole ledger and recompute every signature in-chain. Verifies
   * with the same key used to construct this instance.
   */
  verify(): VerifyResult {
    return verifyLedger(this.path, this.key, this.baseDir);
  }
}

/**
 * Verify a ledger file without constructing an {@link AuditLedger}. Re-derives
 * each signature from the stored content + the running `prev_sig` and returns
 * the index of the first line whose signature or chain link is broken.
 *
 * `path` is resolved and must stay under `baseDir` (default `process.cwd()`).
 */
export function verifyLedger(
  path: string,
  key?: string,
  baseDir?: string,
): VerifyResult {
  const confined = confinePath(path, resolve(baseDir ?? process.cwd()));
  const resolvedKey = resolveKey(key);
  const raw = existsSync(confined) ? readFileSync(confined, "utf-8") : "";
  const records = parseLines(raw);

  let prevSig = "";
  for (let i = 0; i < records.length; i++) {
    const rec = records[i]!;
    if (rec.prev_sig !== prevSig) {
      return {
        ok: false,
        tamperedIndex: i,
        reason: `prev_sig mismatch: chain broken at line ${i}`,
      };
    }
    const { sig, ...unsigned } = rec;
    const expected = sign(resolvedKey, unsigned);
    if (expected !== sig) {
      return {
        ok: false,
        tamperedIndex: i,
        reason: `signature mismatch at line ${i}`,
      };
    }
    prevSig = sig;
  }
  return { ok: true, count: records.length };
}
