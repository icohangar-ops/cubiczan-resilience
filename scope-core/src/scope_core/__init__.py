"""scope-core — shared, domain-agnostic engine for the scope-* data pipelines.

This package generalizes the COMMON code that scope-glacier, scope-sentinel and
scope-vantage each re-implemented: a safe Athena query helper, base ingestion /
analysis handler scaffolds, and small AWS utilities (S3 write, result polling,
Lambda response envelope).

It deliberately keeps no domain knowledge. Allowlists, SQL, column projections
and score logic are all injected by the caller.
"""

from scope_core.athena import (
    AthenaQueryError,
    AthenaTimeoutError,
    IdentifierError,
    SafeAthenaClient,
    validate_identifier,
    validate_in_allowlist,
)
from scope_core.handlers import (
    BaseAnalysisHandler,
    BaseIngestionHandler,
)
from scope_core.utils import (
    error_response,
    poll_until_terminal,
    response_envelope,
    success_response,
    write_jsonl_to_s3,
    write_object_to_s3,
)

__version__ = "0.1.0"

__all__ = [
    "AthenaQueryError",
    "AthenaTimeoutError",
    "IdentifierError",
    "SafeAthenaClient",
    "validate_identifier",
    "validate_in_allowlist",
    "BaseAnalysisHandler",
    "BaseIngestionHandler",
    "error_response",
    "poll_until_terminal",
    "response_envelope",
    "success_response",
    "write_jsonl_to_s3",
    "write_object_to_s3",
    "__version__",
]
