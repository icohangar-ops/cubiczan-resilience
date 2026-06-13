"""Base handler scaffolds for the scope-* pipelines.

Two abstract base classes capture the flows the three repos share, while keeping
all domain specifics (which external API to call, which SQL to run, which columns
to project, which allowlist to enforce) as injected hooks:

* :class:`BaseIngestionHandler` — fetch external records, write them to S3.
  Subclasses implement :meth:`fetch_records` and supply S3 location config.
* :class:`BaseAnalysisHandler` — for each caller-supplied entity, validate it
  against the domain allowlist and run an injected analysis query via
  :class:`~scope_core.athena.SafeAthenaClient`, collecting rows per entity.

Neither class hardcodes glacier/sentinel/vantage knowledge.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from scope_core.athena import SafeAthenaClient, validate_in_allowlist
from scope_core.utils import error_response, success_response, write_jsonl_to_s3

logger = logging.getLogger(__name__)


class BaseIngestionHandler:
    """Scaffold for the "fetch external data -> write to S3" flow.

    Subclasses must implement :meth:`fetch_records`, returning an iterable of
    flat dict records. The base class handles the S3 JSON-Lines write and the
    response envelope.
    """

    #: Default S3 key used if :meth:`build_s3_key` is not overridden.
    default_key = "records.jsonl"

    def __init__(self, s3: Any, *, bucket: str) -> None:
        self._s3 = s3
        self.bucket = bucket

    def fetch_records(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch records from the external source. Subclasses MUST override."""
        raise NotImplementedError

    def build_s3_key(self, event: Dict[str, Any]) -> str:
        """Return the S3 key for this ingestion run. Override for partitioning."""
        return self.default_key

    def ingest(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Run the full ingest flow and return a result dict (not yet enveloped)."""
        records = list(self.fetch_records(event))
        s3_path = ""
        if records:
            s3_path = write_jsonl_to_s3(
                self._s3,
                bucket=self.bucket,
                key=self.build_s3_key(event),
                records=records,
            )
        return {
            "status": "completed",
            "records_fetched": len(records),
            "s3_path": s3_path,
        }

    def handle(self, event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        """Lambda entry point: run :meth:`ingest`, wrap in a response envelope."""
        try:
            return success_response(self.ingest(event))
        except Exception as exc:  # noqa: BLE001 — top-level Lambda boundary
            logger.error("Ingestion failed: %s", exc)
            return error_response(exc)


class BaseAnalysisHandler:
    """Scaffold for the "validate entities -> run analysis query" flow.

    Parameters
    ----------
    client:
        A :class:`~scope_core.athena.SafeAthenaClient`.
    allowlist:
        The domain allowlist of valid entity identifiers (tickers, commodity
        codes, commodity names). Every entity is validated against this set
        *before* any SQL is built — the canonical SQL-injection fix.
    entity_field:
        Name of the field describing the entity (e.g. ``"ticker"``), used in
        result dicts and error messages.
    """

    def __init__(
        self,
        client: SafeAthenaClient,
        *,
        allowlist: Iterable[Any],
        entity_field: str = "entity",
    ) -> None:
        self.client = client
        self.allowlist = (
            allowlist if isinstance(allowlist, (set, frozenset)) else set(allowlist)
        )
        self.entity_field = entity_field

    def build_query(self, entity: Any, event: Dict[str, Any]) -> Tuple[str, Sequence[str]]:
        """Return ``(sql, parameters)`` for one validated entity.

        Subclasses MUST override. ``entity`` has already passed the allowlist
        check, so it is safe to interpolate; any *other* free-text values should
        be returned in ``parameters`` and referenced as ``?`` placeholders.
        """
        raise NotImplementedError

    def analyze_entity(self, entity: Any, event: Dict[str, Any]) -> Dict[str, Any]:
        """Validate one entity against the allowlist and run its analysis query."""
        validate_in_allowlist(entity, self.allowlist, field=self.entity_field)
        query, parameters = self.build_query(entity, event)
        result = self.client.execute(query, parameters=parameters)
        return {
            self.entity_field: entity,
            "query_execution_id": result["query_execution_id"],
            "status": "completed",
            "rows": result["rows"],
        }

    def analyze(
        self, entities: Sequence[Any], event: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Analyze each entity, capturing per-entity errors without aborting."""
        event = event or {}
        results: List[Dict[str, Any]] = []
        for entity in entities:
            try:
                results.append(self.analyze_entity(entity, event))
            except Exception as exc:  # noqa: BLE001 — per-entity isolation
                logger.error("Error analyzing %s %r: %s", self.entity_field, entity, exc)
                results.append(
                    {self.entity_field: entity, "status": "error", "error": str(exc)}
                )
        return results
