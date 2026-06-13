# resilient-onchain

A chain-library-agnostic **resilient on-chain write helper**, shipped as two
parallel implementations:

- [`typescript/`](./typescript) — ESM, tested with `vitest` + `tsc`.
- [`python/`](./python) — synchronous, dependency-free, tested with `pytest`.

The signer/provider is **injected** — nothing here imports `ethers`, `web3`,
`viem`, or `web3.py`. You supply thin callbacks; the helper owns the resilience
logic. Adapter examples for **ethers** (TS) and **web3.py** (Python) are below.

## What it gives you

1. **`sendWithRetry(fetchState, signSend, opts)`** — bounded retry with a
   gas/fee **bump per attempt** (supports both legacy `gasPrice` and EIP-1559
   `maxFeePerGas`/`maxPriorityFeePerGas` via the `feeMode` flag), **nonce
   management** (fetch-once, pin across replacement attempts, refresh on
   `nonce too low`), and an **explicit tx-success assertion** — it throws
   `TxFailedError` if `receipt.status != success` rather than silently
   continuing past a reverted tx.
2. **Off-chain circuit breaker** — `CircuitBreaker(threshold, cooldown)` stops
   submitting after N consecutive failures, so a failing chain/RPC can't drain
   your gas. Optional `cooldown` enables a half-open trial after a delay.
3. **Idempotency guard** — supply `idempotencyKey` + `alreadySent(key)` and a
   crashed-then-retried job that already broadcast will short-circuit
   (`ALREADY_SENT`) **before** spending any gas.

### Provenance (patterns lifted, not copied)

| Pattern | Source repo |
|---|---|
| Pinned nonce + retry-with-gas-bump (`int(gas*factor)+1`), propagate on exhaustion | `critmin-oracle` `push_to_chain` |
| Per-attempt wall-clock timeout + explicit `status != success` assertion | `deepbook-trading-agent` `withTimeoutAndRetry` / `waitForTransaction` |
| Idempotency check before the on-chain transfer | `cleanmandate` `find_completed_transfer` |

---

## TypeScript

```bash
cd typescript
npm install
npm run build      # tsc -> dist/
npm test           # vitest run
```

### ethers v6 adapter

```ts
import { ethers } from 'ethers';
import {
  sendWithRetry,
  CircuitBreaker,
  type FetchState,
  type SignSend,
  type NormalizedReceipt,
} from '@cubiczan/resilient-onchain';

const provider = new ethers.JsonRpcProvider(process.env.RPC_URL);
const wallet = new ethers.Wallet(process.env.PK!, provider);

// Fetch nonce + current fee data ONCE; the helper pins + bumps from here.
const fetchState: FetchState = async () => {
  const [nonce, feeData] = await Promise.all([
    provider.getTransactionCount(wallet.address, 'pending'),
    provider.getFeeData(),
  ]);
  // EIP-1559 example; for legacy use { gasPrice: feeData.gasPrice! }.
  return {
    nonce,
    fee: {
      maxFeePerGas: feeData.maxFeePerGas!,
      maxPriorityFeePerGas: feeData.maxPriorityFeePerGas!,
    },
  };
};

// Build + sign + send ONE attempt using the helper-supplied nonce + fee.
const signSend: SignSend = async (ctx) => {
  const tx = await wallet.sendTransaction({
    to: '0xRecipient',
    value: ethers.parseEther('0.01'),
    nonce: ctx.nonce,                       // pinned by the helper
    maxFeePerGas: ctx.fee.maxFeePerGas,     // bumped each retry
    maxPriorityFeePerGas: ctx.fee.maxPriorityFeePerGas,
  });
  return {
    txHash: tx.hash,
    wait: async (): Promise<NormalizedReceipt> => {
      const rcpt = await provider.waitForTransaction(tx.hash, 1, 120_000);
      if (!rcpt) throw new Error('timeout waiting for receipt'); // transient -> retry
      return { status: rcpt.status ?? 0, txHash: tx.hash, raw: rcpt };
    },
  };
};

const breaker = new CircuitBreaker(5);     // open after 5 consecutive failures
const seen = new Set<string>();            // back this with durable storage

const receipt = await sendWithRetry(fetchState, signSend, {
  feeMode: 'eip1559',
  bumpFactor: 1.125,                        // +12.5% each retry (and +1 wei floor)
  maxAttempts: 5,
  breaker,
  idempotencyKey: 'payout:job-42',
  alreadySent: (k) => seen.has(k),
});
console.log('confirmed', receipt.txHash);
```

---

## Python

```bash
cd python
python3 -m pip install -e ".[test]"   # or just have pytest available
python3 -m pytest                      # tests (mocked provider)
python3 -m py_compile resilient_onchain/*.py
```

### web3.py adapter

```python
from web3 import Web3
from eth_account import Account
from resilient_onchain import (
    send_with_retry, SendOpts, StateResult, SubmitResult,
    FeeFields, NormalizedReceipt, TxContext, CircuitBreaker,
)

w3 = Web3(Web3.HTTPProvider(RPC_URL))
acct = Account.from_key(PK)

def fetch_state() -> StateResult:
    # Fetch nonce + fee ONCE; the helper pins + bumps from here.
    return StateResult(
        nonce=w3.eth.get_transaction_count(acct.address),
        fee=FeeFields(gas_price=w3.eth.gas_price),   # legacy mode
    )

def sign_send(ctx: TxContext) -> SubmitResult:
    tx = {
        "to": "0xRecipient",
        "value": w3.to_wei(0.01, "ether"),
        "gas": 21000,
        "gasPrice": ctx.fee.gas_price,   # bumped each retry
        "nonce": ctx.nonce,              # pinned by the helper
        "chainId": w3.eth.chain_id,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)

    def wait() -> NormalizedReceipt:
        # native 120s timeout; raises on timeout -> helper retries w/ bumped gas
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return NormalizedReceipt(status=rcpt["status"], tx_hash=tx_hash.hex(), raw=rcpt)

    return SubmitResult(tx_hash=tx_hash.hex(), wait=wait)

breaker = CircuitBreaker(threshold=5)
seen = set()   # back this with durable storage

receipt = send_with_retry(
    fetch_state, sign_send,
    SendOpts(
        fee_mode="legacy",
        bump_factor=1.125,
        max_attempts=5,
        breaker=breaker,
        idempotency_key="payout:job-42",
        already_sent=lambda k: k in seen,
    ),
)
print("confirmed", receipt.tx_hash)
```

> EIP-1559 in web3.py: return `FeeFields(max_fee_per_gas=..., max_priority_fee_per_gas=...)`
> from `fetch_state`, pass `fee_mode="eip1559"`, and set `maxFeePerGas` /
> `maxPriorityFeePerGas` (instead of `gasPrice`) on the tx dict in `sign_send`.

---

## Behavior notes

- **Pinned nonce:** fetched once, reused across replacement attempts so a
  bumped-fee resend *replaces* the prior dropped/underpriced tx instead of
  queuing a second one. On `nonce too low` the helper re-fetches state and
  retries with the fresh nonce.
- **Strictly increasing fee:** each bump floors `fee * factor` then adds 1 wei,
  guaranteeing the replacement exceeds the chain's minimum-bump requirement.
- **Fail-fast:** deterministic errors (`insufficient funds`, `invalid
  signature`, `execution reverted`) raise immediately and do not consume the
  retry/backoff budget.
- **Circuit breaker is opt-in and shareable** across calls — pass the same
  instance to enforce a global failure budget for one signer/chain.
- **Idempotency storage is yours:** `alreadySent` should consult durable
  state (DB row, ledger, KV) written when a tx is first broadcast.
