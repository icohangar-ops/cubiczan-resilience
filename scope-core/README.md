# scope-core

Shared, **domain-agnostic** engine extracted from the three sibling pipelines
`scope-glacier`, `scope-sentinel` and `scope-vantage`. Those repos each
re-implemented the same architecture — ingest external data → write to S3/Glue,
then run Athena queries to produce analysis — and each independently re-fixed an
identical SQL-injection class (f-string interpolation of caller-controlled values
into Athena SQL). `scope-core` generalizes that common code in one place.

It holds **no** glacier/sentinel/vantage specifics. Allowlists, SQL, column
projections and score logic are all injected by the caller.

## Install

```bash
pip install scope-core      # requires Python >= 3.10, boto3
```

## What's inside

| Module | Purpose |
| ------ | ------- |
| `scope_core.athena` | `SafeAthenaClient`, identifier allowlist/validation, query errors |
| `scope_core.handlers` | `BaseIngestionHandler`, `BaseAnalysisHandler` scaffolds |
| `scope_core.utils` | S3 write, generic state poller, Lambda response envelope |

## 1. Safe Athena queries (the SQL-injection fix)

The canonical defense is the allowlist: any caller-controlled identifier is
checked against the **domain's** allowed set *before* it can reach a SQL string.
Free-text literals go through Athena execution parameters (`?` placeholders),
never string formatting.

```python
import boto3
from scope_core import SafeAthenaClient, validate_in_allowlist

ALLOWED_TICKERS = {"O", "PLD", "SPG"}

client = SafeAthenaClient(
    boto3.client("athena"),
    database="scope_sentinel",
    output_location="s3://scope-sentinel-queries/",
    poll_timeout=120,
)

ticker = validate_in_allowlist(user_input, ALLOWED_TICKERS, field="ticker")
# attacker payloads like "O'; DROP TABLE reits;--" raise IdentifierError here

result = client.execute(
    f"SELECT * FROM scope_sentinel.reits WHERE ticker = '{ticker}' AND name = ?",
    parameters=[some_free_text_name],   # bound, not formatted
)
print(result["state"], result["rows"])
```

`execute()` starts the query, polls `GetQueryExecution` until a terminal state
(raising `AthenaQueryError` on FAILED/CANCELLED and `AthenaTimeoutError` on
timeout), then returns rows.

## 2. Base ingestion handler

```python
from scope_core import BaseIngestionHandler

class EiaIngestion(BaseIngestionHandler):
    def build_s3_key(self, event):
        return f"eia/prices/{event['date']}/spot_prices.jsonl"

    def fetch_records(self, event):
        return fetch_eia_prices(event["commodities"])   # your domain code

def handler(event, context):
    return EiaIngestion(boto3.client("s3"), bucket="scope-glacier-raw").handle(event)
```

The base class writes the records as JSON Lines to S3 and returns the
`{"statusCode", "body"}` envelope.

## 3. Base analysis handler

```python
from scope_core import BaseAnalysisHandler, SafeAthenaClient

class GlacierAnalysis(BaseAnalysisHandler):
    def build_query(self, code, event):
        # `code` already passed the allowlist; free text goes in parameters
        sql = f"SELECT '{code}' AS commodity_code, ... FROM scope_glacier.price_series WHERE commodity_code = '{code}'"
        return sql, []

handler = GlacierAnalysis(
    SafeAthenaClient(boto3.client("athena"),
                     database="scope_glacier",
                     output_location="s3://scope-glacier-queries/"),
    allowlist={"WTI", "BRENT", "HH"},
    entity_field="commodity_code",
)
signals = handler.analyze(event["commodity_codes"], event)
```

`analyze()` validates each entity against the allowlist, runs the injected query,
and collects rows per entity, isolating per-entity errors.

## Testing

```bash
python -m pytest        # boto3 is mocked; no live AWS required
```
