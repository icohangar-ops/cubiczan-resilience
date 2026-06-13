import { describe, it, expect, vi } from 'vitest';
import {
  sendWithRetry,
  CircuitBreaker,
  CircuitOpenError,
  TxFailedError,
  RetryExhaustedError,
  OnchainWriteError,
  bumpFee,
  initialFee,
  isReceiptSuccess,
  isNonceTooLow,
  type FetchState,
  type SignSend,
  type NormalizedReceipt,
  type FeeFields,
} from '../src/index.js';

const noSleep = async () => {};
const okReceipt: NormalizedReceipt = { status: 1, txHash: '0xok' };
const badReceipt: NormalizedReceipt = { status: 0, txHash: '0xbad' };

function legacyState(nonce = 7, gasPrice = 100n): FetchState {
  return async () => ({ nonce, fee: { gasPrice } });
}

describe('isReceiptSuccess', () => {
  it('accepts numeric/boolean/string success forms', () => {
    expect(isReceiptSuccess({ status: 1 })).toBe(true);
    expect(isReceiptSuccess({ status: true })).toBe(true);
    expect(isReceiptSuccess({ status: 'success' })).toBe(true);
    expect(isReceiptSuccess({ status: 0 })).toBe(false);
    expect(isReceiptSuccess({ status: false })).toBe(false);
    expect(isReceiptSuccess({ status: 'reverted' })).toBe(false);
  });
});

describe('fee bump', () => {
  it('strictly increases legacy gasPrice each attempt (+1 wei floor)', () => {
    const f0 = initialFee('legacy', { gasPrice: 100n });
    const f1 = bumpFee('legacy', f0, 1.125);
    const f2 = bumpFee('legacy', f1, 1.125);
    expect(f1.gasPrice!).toBeGreaterThan(f0.gasPrice!);
    expect(f2.gasPrice!).toBeGreaterThan(f1.gasPrice!);
    // 100 * 1.125 = 112.5 -> floor 112 + 1 = 113
    expect(f1.gasPrice).toBe(113n);
  });

  it('bumps both eip1559 fields', () => {
    const f0 = initialFee('eip1559', { maxFeePerGas: 200n, maxPriorityFeePerGas: 10n });
    const f1 = bumpFee('eip1559', f0, 1.2);
    expect(f1.maxFeePerGas!).toBeGreaterThan(f0.maxFeePerGas!);
    expect(f1.maxPriorityFeePerGas!).toBeGreaterThan(f0.maxPriorityFeePerGas!);
  });

  it('even a tiny fee bumps by at least 1 wei', () => {
    const f1 = bumpFee('legacy', { gasPrice: 1n }, 1.01);
    expect(f1.gasPrice!).toBeGreaterThan(1n);
  });
});

describe('sendWithRetry — happy path', () => {
  it('returns receipt on first-attempt success', async () => {
    const signSend = vi.fn<SignSend>(async () => ({
      txHash: '0xok',
      wait: async () => okReceipt,
    }));
    const r = await sendWithRetry(legacyState(), signSend, { sleep: noSleep });
    expect(r).toBe(okReceipt);
    expect(signSend).toHaveBeenCalledTimes(1);
  });
});

describe('sendWithRetry — retry then success + gas bump per attempt', () => {
  it('retries on transient error and bumps fee strictly each attempt', async () => {
    const seenGasPrices: bigint[] = [];
    let calls = 0;
    const signSend: SignSend = async (ctx) => {
      seenGasPrices.push(ctx.fee.gasPrice!);
      calls += 1;
      if (calls < 3) {
        return {
          txHash: `0xdrop${calls}`,
          wait: async () => {
            throw new Error('timeout waiting for receipt'); // transient
          },
        };
      }
      return { txHash: '0xok', wait: async () => okReceipt };
    };

    const r = await sendWithRetry(legacyState(7, 100n), signSend, {
      maxAttempts: 5,
      feeMode: 'legacy',
      bumpFactor: 1.5,
      sleep: noSleep,
    });

    expect(r).toBe(okReceipt);
    expect(calls).toBe(3);
    // Three submissions, each strictly higher than the last.
    expect(seenGasPrices.length).toBe(3);
    expect(seenGasPrices[1]).toBeGreaterThan(seenGasPrices[0]);
    expect(seenGasPrices[2]).toBeGreaterThan(seenGasPrices[1]);
  });
});

describe('sendWithRetry — nonce-too-low triggers refresh', () => {
  it('refreshes nonce via fetchState and retries with the new nonce', async () => {
    const fetchState = vi
      .fn<FetchState>()
      .mockResolvedValueOnce({ nonce: 7, fee: { gasPrice: 100n } })
      .mockResolvedValueOnce({ nonce: 9, fee: { gasPrice: 150n } });

    const seenNonces: number[] = [];
    let calls = 0;
    const signSend: SignSend = async (ctx) => {
      seenNonces.push(ctx.nonce);
      calls += 1;
      if (calls === 1) {
        return {
          txHash: '0xstale',
          wait: async () => {
            throw new Error('nonce too low');
          },
        };
      }
      return { txHash: '0xok', wait: async () => okReceipt };
    };

    const r = await sendWithRetry(fetchState, signSend, { sleep: noSleep });
    expect(r).toBe(okReceipt);
    expect(fetchState).toHaveBeenCalledTimes(2); // initial + refresh
    expect(seenNonces).toEqual([7, 9]); // second attempt uses refreshed nonce
  });

  it('detects various nonce-too-low phrasings', () => {
    expect(isNonceTooLow(new Error('nonce too low'))).toBe(true);
    expect(isNonceTooLow(new Error('Nonce is too low'))).toBe(true);
    expect(isNonceTooLow(new Error('invalid nonce'))).toBe(true);
    expect(isNonceTooLow(new Error('some other error'))).toBe(false);
  });
});

describe('sendWithRetry — success assertion throws on reverted receipt', () => {
  it('throws TxFailedError when receipt status is failure (no silent continue)', async () => {
    const signSend: SignSend = async () => ({
      txHash: '0xbad',
      wait: async () => badReceipt,
    });
    await expect(
      sendWithRetry(legacyState(), signSend, { sleep: noSleep }),
    ).rejects.toBeInstanceOf(TxFailedError);
  });
});

describe('sendWithRetry — retry exhaustion', () => {
  it('throws RetryExhaustedError after maxAttempts transient failures', async () => {
    const signSend: SignSend = async () => ({
      txHash: '0xnope',
      wait: async () => {
        throw new Error('timeout'); // always transient
      },
    });
    await expect(
      sendWithRetry(legacyState(), signSend, { maxAttempts: 3, sleep: noSleep }),
    ).rejects.toBeInstanceOf(RetryExhaustedError);
  });

  it('fails fast (non-retryable) on insufficient funds', async () => {
    const signSend = vi.fn<SignSend>(async () => ({
      txHash: '0x',
      wait: async () => {
        throw new Error('insufficient funds for gas');
      },
    }));
    await expect(
      sendWithRetry(legacyState(), signSend, { maxAttempts: 5, sleep: noSleep }),
    ).rejects.toMatchObject({ code: 'NON_RETRYABLE' });
    expect(signSend).toHaveBeenCalledTimes(1);
  });
});

describe('CircuitBreaker', () => {
  it('opens after N consecutive failures and blocks submission', async () => {
    const breaker = new CircuitBreaker(2);
    const signSend: SignSend = async () => ({
      txHash: '0x',
      wait: async () => {
        throw new Error('timeout');
      },
    });

    // First call: 3 attempts all fail -> failures accumulate past threshold.
    await expect(
      sendWithRetry(legacyState(), signSend, { maxAttempts: 3, breaker, sleep: noSleep }),
    ).rejects.toBeInstanceOf(RetryExhaustedError);
    expect(breaker.isOpen).toBe(true);

    // Second call should be rejected by the OPEN breaker before any submit.
    const signSend2 = vi.fn<SignSend>(async () => ({ txHash: '0x', wait: async () => okReceipt }));
    await expect(
      sendWithRetry(legacyState(), signSend2, { breaker, sleep: noSleep }),
    ).rejects.toBeInstanceOf(CircuitOpenError);
    expect(signSend2).not.toHaveBeenCalled();
  });

  it('resets on success', () => {
    const breaker = new CircuitBreaker(2);
    breaker.recordFailure();
    expect(breaker.failures).toBe(1);
    breaker.recordSuccess();
    expect(breaker.failures).toBe(0);
    expect(breaker.isOpen).toBe(false);
  });

  it('half-opens after cooldown', () => {
    let t = 1000;
    const breaker = new CircuitBreaker(1, 5000, () => t);
    breaker.recordFailure();
    expect(breaker.isOpen).toBe(true);
    t += 6000;
    expect(breaker.isOpen).toBe(false); // cooldown elapsed -> half-open
  });
});

describe('sendWithRetry — idempotency guard', () => {
  it('blocks duplicate submission when alreadySent returns true', async () => {
    const signSend = vi.fn<SignSend>(async () => ({ txHash: '0x', wait: async () => okReceipt }));
    await expect(
      sendWithRetry(legacyState(), signSend, {
        idempotencyKey: 'job-42',
        alreadySent: async (k) => k === 'job-42',
        sleep: noSleep,
      }),
    ).rejects.toMatchObject({ code: 'ALREADY_SENT' });
    expect(signSend).not.toHaveBeenCalled();
  });

  it('proceeds when alreadySent returns false', async () => {
    const signSend = vi.fn<SignSend>(async () => ({ txHash: '0xok', wait: async () => okReceipt }));
    const r = await sendWithRetry(legacyState(), signSend, {
      idempotencyKey: 'job-99',
      alreadySent: async () => false,
      sleep: noSleep,
    });
    expect(r).toBe(okReceipt);
    expect(signSend).toHaveBeenCalledTimes(1);
  });
});

describe('config validation', () => {
  it('rejects bumpFactor <= 1', async () => {
    await expect(
      sendWithRetry(legacyState(), async () => ({ txHash: '0x', wait: async () => okReceipt }), {
        bumpFactor: 1,
        sleep: noSleep,
      }),
    ).rejects.toBeInstanceOf(OnchainWriteError);
  });
});
