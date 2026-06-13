/**
 * Resilient on-chain write client (chain-library-agnostic).
 *
 * Lifts and generalizes three proven patterns from production repos:
 *   - critmin-oracle  : bounded retry + gas/fee bump per attempt, pinned nonce,
 *                        refresh on nonce-too-low, propagate after exhaustion.
 *   - deepbook-agent  : per-attempt wall-clock timeout, explicit tx-success
 *                        assertion (throw if receipt status != success).
 *   - cleanmandate    : idempotency guard before submit (no double-send on a
 *                        crashed-then-retried job).
 *
 * The signer/provider is INJECTED. Nothing here imports ethers / web3 / viem;
 * callers supply thin adapter callbacks (see README for ethers + web3.py).
 */

/* ─── Errors ───────────────────────────────────────────────────────── */

export class OnchainWriteError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly cause?: unknown,
  ) {
    super(message);
    this.name = 'OnchainWriteError';
  }
}

/** Thrown when the receipt confirms but reports a reverted/failed tx. */
export class TxFailedError extends OnchainWriteError {
  constructor(
    message: string,
    public readonly receipt?: unknown,
  ) {
    super(message, 'TX_FAILED', undefined);
    this.name = 'TxFailedError';
  }
}

/** Thrown when the circuit breaker is open and refuses to submit. */
export class CircuitOpenError extends OnchainWriteError {
  constructor(message: string) {
    super(message, 'CIRCUIT_OPEN');
    this.name = 'CircuitOpenError';
  }
}

/** Thrown when retries are exhausted without a successful confirmation. */
export class RetryExhaustedError extends OnchainWriteError {
  constructor(message: string, cause?: unknown) {
    super(message, 'RETRY_EXHAUSTED', cause);
    this.name = 'RetryExhaustedError';
  }
}

/* ─── Fee modes ────────────────────────────────────────────────────── */

export type FeeMode = 'legacy' | 'eip1559';

/**
 * Fee fields injected into the tx for a given attempt. In `legacy` mode only
 * `gasPrice` is set; in `eip1559` mode `maxFeePerGas` + `maxPriorityFeePerGas`
 * are set. Values are bigint to avoid float drift on wei amounts.
 */
export interface FeeFields {
  gasPrice?: bigint;
  maxFeePerGas?: bigint;
  maxPriorityFeePerGas?: bigint;
}

/* ─── Receipt shape ────────────────────────────────────────────────── */

/**
 * Minimal receipt contract the helper understands. Adapters normalize their
 * native receipt to this. `status` of 1 / true / 'success' means success;
 * 0 / false / 'failure'/'reverted' means the tx reverted on-chain.
 */
export interface NormalizedReceipt {
  status: number | boolean | string;
  txHash?: string;
  raw?: unknown;
}

/** True iff the receipt indicates an on-chain success. */
export function isReceiptSuccess(r: NormalizedReceipt): boolean {
  const s = r.status;
  if (typeof s === 'number') return s === 1;
  if (typeof s === 'boolean') return s;
  const norm = String(s).toLowerCase();
  return norm === 'success' || norm === '1' || norm === 'true' || norm === 'ok';
}

/* ─── Tx context passed to caller callbacks ────────────────────────── */

/**
 * Everything the caller needs to build + sign + send one attempt. The helper
 * owns nonce + fee selection; the caller owns the actual chain calls.
 */
export interface TxContext {
  /** Pinned nonce for this logical tx (same across replacement attempts). */
  nonce: number;
  /** Fee fields for this attempt (bumped each retry). */
  fee: FeeFields;
  /** 1-based attempt number. */
  attempt: number;
}

/** Result of a single signSend: the tx hash + a way to await its receipt. */
export interface SubmitResult {
  txHash: string;
  /** Resolve to the normalized receipt, or reject on timeout/error. */
  wait: () => Promise<NormalizedReceipt>;
}

/* ─── Circuit breaker ──────────────────────────────────────────────── */

/**
 * Off-chain circuit breaker: after `threshold` consecutive failures it opens
 * and rejects further submissions for `cooldownMs`, preventing gas from being
 * drained into a failing chain/RPC. A success resets the failure count.
 */
export class CircuitBreaker {
  private consecutiveFailures = 0;
  private openedAt: number | null = null;

  constructor(
    private readonly threshold: number,
    private readonly cooldownMs: number = 0,
    private readonly now: () => number = () => Date.now(),
  ) {
    if (threshold < 1) throw new OnchainWriteError('threshold must be >= 1', 'BAD_CONFIG');
  }

  /** Throws CircuitOpenError if the breaker is currently open. */
  assertClosed(): void {
    if (this.openedAt === null) return;
    if (this.cooldownMs > 0 && this.now() - this.openedAt >= this.cooldownMs) {
      // Half-open: allow one trial through; failure re-opens immediately.
      return;
    }
    throw new CircuitOpenError(
      `circuit open after ${this.consecutiveFailures} consecutive failures`,
    );
  }

  recordSuccess(): void {
    this.consecutiveFailures = 0;
    this.openedAt = null;
  }

  recordFailure(): void {
    this.consecutiveFailures += 1;
    if (this.consecutiveFailures >= this.threshold) {
      this.openedAt = this.now();
    }
  }

  get isOpen(): boolean {
    try {
      this.assertClosed();
      return false;
    } catch {
      return true;
    }
  }

  get failures(): number {
    return this.consecutiveFailures;
  }
}

/* ─── Options ──────────────────────────────────────────────────────── */

export interface SendWithRetryOpts {
  /** Max attempts (initial + retries). Default 5. */
  maxAttempts?: number;
  /** Fee mode: legacy gasPrice or eip1559 maxFeePerGas. Default 'legacy'. */
  feeMode?: FeeMode;
  /** Multiplier applied to fee fields each retry (>1). Default 1.125 (~12.5%). */
  bumpFactor?: number;
  /** Per-attempt wall-clock timeout in ms for build+sign+send+wait. Default 120_000. */
  timeoutMs?: number;
  /** Base backoff between attempts in ms (doubled each retry). Default 1_000. */
  backoffBaseMs?: number;
  /**
   * Idempotency key. If provided together with `alreadySent`, the helper checks
   * it BEFORE the first submission and short-circuits if already sent.
   */
  idempotencyKey?: string;
  /** Returns true if a tx for `key` was already submitted (crash-recovery). */
  alreadySent?: (key: string) => boolean | Promise<boolean>;
  /** Shared circuit breaker. If omitted, no cross-call breaker is enforced. */
  breaker?: CircuitBreaker;
  /** Optional logger; defaults to no-op. */
  logger?: (msg: string) => void;
  /** Injected clock + sleep (for tests). */
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

/* ─── Fee initialization + bump ────────────────────────────────────── */

/** Build the fee fields for attempt 1 from current chain fee data. */
export function initialFee(mode: FeeMode, base: FeeFields): FeeFields {
  if (mode === 'legacy') {
    if (base.gasPrice === undefined)
      throw new OnchainWriteError('legacy mode requires gasPrice', 'BAD_FEE');
    return { gasPrice: base.gasPrice };
  }
  if (base.maxFeePerGas === undefined)
    throw new OnchainWriteError('eip1559 mode requires maxFeePerGas', 'BAD_FEE');
  return {
    maxFeePerGas: base.maxFeePerGas,
    // Default priority fee to maxFee if unsupplied.
    maxPriorityFeePerGas: base.maxPriorityFeePerGas ?? base.maxFeePerGas,
  };
}

/**
 * Bump fee for the next attempt. Mirrors critmin's `int(gas*factor)+1`: floor
 * then +1 wei so a replacement tx ALWAYS strictly exceeds the prior fee (chains
 * reject same-nonce replacements that don't raise the fee enough).
 */
export function bumpFee(mode: FeeMode, fee: FeeFields, factor: number): FeeFields {
  const bump = (v: bigint): bigint => {
    // bigint * float: scale factor to integer math to avoid float drift.
    const scaled = (v * BigInt(Math.round(factor * 1000))) / 1000n;
    return scaled + 1n;
  };
  if (mode === 'legacy') {
    return { gasPrice: bump(fee.gasPrice!) };
  }
  return {
    maxFeePerGas: bump(fee.maxFeePerGas!),
    maxPriorityFeePerGas: bump(fee.maxPriorityFeePerGas ?? fee.maxFeePerGas!),
  };
}

/* ─── nonce-too-low detection ──────────────────────────────────────── */

/** Heuristic: does this error mean our pinned nonce is stale? */
export function isNonceTooLow(err: unknown): boolean {
  const msg = ((err as Error)?.message ?? String(err)).toLowerCase();
  return (
    msg.includes('nonce too low') ||
    msg.includes('nonce-too-low') ||
    msg.includes('nonce is too low') ||
    msg.includes('already known') ||
    msg.includes('invalid nonce')
  );
}

/** Heuristic: is this a transient error worth retrying (vs. deterministic)? */
export function isRetryable(err: unknown): boolean {
  const msg = ((err as Error)?.message ?? String(err)).toLowerCase();
  const nonRetryable = ['insufficient funds', 'invalid signature', 'execution reverted', 'unauthorized'];
  if (nonRetryable.some((s) => msg.includes(s))) return false;
  return true;
}

/* ─── Per-attempt timeout wrapper (deepbook pattern) ───────────────── */

async function withTimeout<T>(
  op: () => Promise<T>,
  ms: number,
  label: string,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(
        () => reject(new OnchainWriteError(`${label} timed out after ${ms}ms`, 'TIMEOUT')),
        ms,
      );
    });
    return await Promise.race([op(), timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/* ─── Caller callbacks ─────────────────────────────────────────────── */

/**
 * Fetch current on-chain state needed to start: the account nonce and base fee
 * data. Called ONCE before the retry loop (nonce is then pinned + bumped
 * locally), and again only on a nonce-too-low refresh.
 */
export type FetchState = () => Promise<{ nonce: number; fee: FeeFields }>;

/**
 * Build + sign + broadcast one attempt using the supplied context, returning a
 * tx hash and a `wait()` that resolves to a normalized receipt. The adapter
 * decides how to merge `ctx.nonce` / `ctx.fee` into the tx request.
 */
export type SignSend = (ctx: TxContext) => Promise<SubmitResult>;

/* ─── Core: sendWithRetry ──────────────────────────────────────────── */

/**
 * Submit an on-chain write with bounded retry, per-attempt fee bump, pinned
 * nonce (refreshed on nonce-too-low), explicit success assertion, an optional
 * circuit breaker, and an optional idempotency guard.
 *
 * Resolves to the successful NormalizedReceipt; throws on exhaustion, open
 * circuit, deterministic error, or confirmed-but-reverted tx.
 */
export async function sendWithRetry(
  fetchState: FetchState,
  signSend: SignSend,
  opts: SendWithRetryOpts = {},
): Promise<NormalizedReceipt> {
  const maxAttempts = opts.maxAttempts ?? 5;
  const feeMode = opts.feeMode ?? 'legacy';
  const bumpFactor = opts.bumpFactor ?? 1.125;
  const timeoutMs = opts.timeoutMs ?? 120_000;
  const backoffBaseMs = opts.backoffBaseMs ?? 1_000;
  const log = opts.logger ?? (() => {});
  const sleep = opts.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  const breaker = opts.breaker;

  if (bumpFactor <= 1) throw new OnchainWriteError('bumpFactor must be > 1', 'BAD_CONFIG');
  if (maxAttempts < 1) throw new OnchainWriteError('maxAttempts must be >= 1', 'BAD_CONFIG');

  // Idempotency guard (cleanmandate pattern): bail BEFORE spending any gas.
  if (opts.idempotencyKey !== undefined && opts.alreadySent) {
    const sent = await opts.alreadySent(opts.idempotencyKey);
    if (sent) {
      throw new OnchainWriteError(
        `idempotent: tx for key '${opts.idempotencyKey}' already submitted`,
        'ALREADY_SENT',
      );
    }
  }

  // Circuit breaker check (off-chain): refuse to submit into a failing chain.
  if (breaker) breaker.assertClosed();

  // Pin nonce + fee once (critmin pattern). Same nonce across replacements.
  let { nonce, fee: baseFee } = await fetchState();
  let fee = initialFee(feeMode, baseFee);

  let lastErr: unknown;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const receipt = await withTimeout(
        async () => {
          const submitted = await signSend({ nonce, fee, attempt });
          log(`attempt ${attempt}/${maxAttempts} sent nonce=${nonce} hash=${submitted.txHash}`);
          return submitted.wait();
        },
        timeoutMs,
        `attempt ${attempt}`,
      );

      // Explicit success assertion (deepbook pattern): never silently continue
      // past a reverted tx.
      if (!isReceiptSuccess(receipt)) {
        const e = new TxFailedError(
          `tx confirmed but reverted (status=${String(receipt.status)})`,
          receipt,
        );
        // A revert is deterministic for these inputs: do not burn retries.
        breaker?.recordFailure();
        throw e;
      }

      breaker?.recordSuccess();
      log(`attempt ${attempt} confirmed success hash=${receipt.txHash ?? ''}`);
      return receipt;
    } catch (err) {
      lastErr = err;

      // A confirmed revert is terminal — propagate immediately.
      if (err instanceof TxFailedError) throw err;

      // Nonce drifted (another tx landed): refresh nonce + fee and retry
      // WITHOUT counting against the breaker too harshly — but still bounded.
      if (isNonceTooLow(err)) {
        log(`attempt ${attempt} nonce-too-low: refreshing nonce`);
        const refreshed = await fetchState();
        nonce = refreshed.nonce;
        fee = initialFee(feeMode, refreshed.fee);
        // Refresh does not consume the bump chain; retry same attempt budget.
        if (attempt < maxAttempts) {
          await sleep(backoffBaseMs);
          continue;
        }
      }

      breaker?.recordFailure();

      // Deterministic failures (bad sig, insufficient funds, revert text) will
      // never succeed on retry — fail fast.
      if (!isRetryable(err)) {
        throw new OnchainWriteError(
          `non-retryable on-chain error on attempt ${attempt}: ${(err as Error)?.message ?? err}`,
          'NON_RETRYABLE',
          err,
        );
      }

      if (attempt < maxAttempts) {
        // Bump fee + resend with the SAME nonce to replace the dropped/
        // underpriced tx (critmin pattern).
        fee = bumpFee(feeMode, fee, bumpFactor);
        const delay = backoffBaseMs * 2 ** (attempt - 1);
        log(
          `attempt ${attempt} failed (${(err as Error)?.message ?? err}); ` +
            `bumping fee + retrying in ${delay}ms`,
        );
        await sleep(delay);
      }
    }
  }

  throw new RetryExhaustedError(
    `on-chain write failed after ${maxAttempts} attempts: ` +
      `${(lastErr as Error)?.message ?? lastErr}`,
    lastErr,
  );
}
