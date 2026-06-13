"""Safe Athena query execution.

This module is the canonical fix for the SQL-injection class that all three
scope-* repos shared (f-string interpolation of caller-controlled values into
Athena SQL). It provides:

* ``validate_in_allowlist`` / ``validate_identifier`` — the only sanctioned ways
  a caller-controlled identifier (commodity code, ticker, commodity name) may be
  turned into something destined for a SQL string.
* ``SafeAthenaClient`` — start a query, poll to a terminal state with a timeout,
  and (optionally) fetch result rows. Free-text *literals* are passed through
  Athena execution parameters (``?`` placeholders) rather than string-formatted.

Dependencies are intentionally light: ``boto3`` (injected, so tests can mock it)
plus the standard library. The timeout/poll semantics mirror the
cubiczan-resilience patterns conceptually without taking a hard dependency.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Terminal Athena query states.
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

# A conservative identifier shape: a SQL identifier is letters/digits/underscore,
# optionally dotted (db.table.column). This is used only when an allowlist is not
# applicable; the preferred path is always ``validate_in_allowlist``.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class IdentifierError(ValueError):
    """Raised when a caller-supplied identifier fails validation."""


class AthenaQueryError(RuntimeError):
    """Raised when an Athena query reaches FAILED or CANCELLED."""


class AthenaTimeoutError(TimeoutError):
    """Raised when an Athena query does not finish within the timeout."""


def validate_in_allowlist(value: Any, allowed: Iterable[Any], *, field: str = "value") -> Any:
    """Return ``value`` iff it is a member of ``allowed``, else raise.

    This is the primary defense against the SQL-injection class. Callers pass the
    domain allowlist (e.g. their ticker / commodity set); anything outside it is
    rejected *before* it can reach a SQL string.
    """
    allowed_set = allowed if isinstance(allowed, (set, frozenset)) else set(allowed)
    if value not in allowed_set:
        raise IdentifierError(f"Disallowed {field}: {value!r}")
    return value


def validate_identifier(value: Any, *, field: str = "identifier") -> str:
    """Validate a bare SQL identifier (table/column/database name) by shape.

    Use this only for trusted-but-dynamic identifiers (e.g. a configured table
    name). For caller-controlled values, prefer :func:`validate_in_allowlist`.
    """
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise IdentifierError(f"Invalid {field}: {value!r}")
    return value


class SafeAthenaClient:
    """Thin, safe wrapper around a boto3 Athena client.

    Parameters
    ----------
    athena:
        A boto3 ``athena`` client (injected so it can be mocked in tests).
    database:
        Default Glue database for queries.
    output_location:
        Default S3 ``OutputLocation`` for query results.
    poll_timeout:
        Seconds to wait for a query to reach a terminal state.
    poll_interval:
        Seconds between ``GetQueryExecution`` polls.
    """

    def __init__(
        self,
        athena: Any,
        *,
        database: str,
        output_location: str,
        poll_timeout: float = 120.0,
        poll_interval: float = 1.0,
    ) -> None:
        self._athena = athena
        self.database = database
        self.output_location = output_location
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval

    # -- low level ---------------------------------------------------------

    def start(
        self,
        query: str,
        *,
        output_location: Optional[str] = None,
        parameters: Optional[Sequence[str]] = None,
    ) -> str:
        """Start a query and return its QueryExecutionId.

        ``parameters`` are bound via Athena's ``ExecutionParameters`` (``?``
        placeholders) so free-text literals are never string-formatted into SQL.
        """
        kwargs: Dict[str, Any] = {
            "QueryString": query,
            "QueryExecutionContext": {"Database": self.database},
            "ResultConfiguration": {
                "OutputLocation": output_location or self.output_location
            },
        }
        if parameters:
            kwargs["ExecutionParameters"] = [str(p) for p in parameters]
        resp = self._athena.start_query_execution(**kwargs)
        return resp["QueryExecutionId"]

    def wait(
        self,
        query_execution_id: str,
        *,
        timeout: Optional[float] = None,
        interval: Optional[float] = None,
    ) -> str:
        """Poll until the query reaches a terminal state.

        Returns the terminal state on success. Raises :class:`AthenaQueryError`
        on FAILED/CANCELLED and :class:`AthenaTimeoutError` if it does not finish
        in time. Without this poll, a query Athena rejects after
        ``start_query_execution`` returns would silently produce no rows and be
        treated as success.
        """
        max_wait = self.poll_timeout if timeout is None else timeout
        step = self.poll_interval if interval is None else interval
        deadline = time.monotonic() + max_wait
        while True:
            resp = self._athena.get_query_execution(
                QueryExecutionId=query_execution_id
            )
            status = resp["QueryExecution"]["Status"]
            state = status["State"]
            if state in _TERMINAL:
                if state != "SUCCEEDED":
                    reason = status.get("StateChangeReason", "no reason given")
                    raise AthenaQueryError(
                        f"Athena query {query_execution_id} {state}: {reason}"
                    )
                return state
            if time.monotonic() >= deadline:
                raise AthenaTimeoutError(
                    f"Athena query {query_execution_id} did not finish within {max_wait}s"
                )
            time.sleep(step)

    def fetch_rows(
        self, query_execution_id: str, *, max_results: int = 1000
    ) -> List[Dict[str, str]]:
        """Fetch result rows as a list of ``{column: value}`` dicts.

        The first Athena result row is the header; it is consumed to build the
        column names and excluded from the returned rows.
        """
        result = self._athena.get_query_results(
            QueryExecutionId=query_execution_id, MaxResults=max_results
        )
        raw_rows = result.get("ResultSet", {}).get("Rows", [])
        if not raw_rows:
            return []
        header = [c.get("VarCharValue", "") for c in raw_rows[0].get("Data", [])]
        rows: List[Dict[str, str]] = []
        for raw in raw_rows[1:]:
            cells = [c.get("VarCharValue") for c in raw.get("Data", [])]
            rows.append(dict(zip(header, cells)))
        return rows

    # -- high level --------------------------------------------------------

    def execute(
        self,
        query: str,
        *,
        parameters: Optional[Sequence[str]] = None,
        output_location: Optional[str] = None,
        fetch: bool = True,
        timeout: Optional[float] = None,
        max_results: int = 1000,
    ) -> Dict[str, Any]:
        """Start a query, wait for completion, and optionally return rows.

        Returns ``{"query_execution_id", "state", "rows"}``. ``rows`` is ``None``
        when ``fetch`` is False (e.g. for INSERT statements).
        """
        qid = self.start(query, output_location=output_location, parameters=parameters)
        state = self.wait(qid, timeout=timeout)
        rows = self.fetch_rows(qid, max_results=max_results) if fetch else None
        return {"query_execution_id": qid, "state": state, "rows": rows}
