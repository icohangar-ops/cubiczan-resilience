"""Fail-closed FastAPI security helpers.

These require the optional ``fastapi`` extra. Importing this module without
FastAPI installed raises a clear :class:`ImportError`.

* :func:`require_auth` — a dependency that validates a bearer token against an
  env-configured secret. It is *fail-closed*: if the env var is unset or empty,
  every request is rejected (so a misconfigured deploy never silently runs
  unauthenticated).
* :func:`cors_allowlist` — a factory returning kwargs for
  ``CORSMiddleware`` that refuses the insecure wildcard-origin +
  credentials combination.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Callable, Optional, Sequence

try:
    from fastapi import Depends, HTTPException, Request, status
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
except ImportError as exc:  # pragma: no cover - exercised via extras
    raise ImportError(
        "cubiczan_resilience.fastapi_helpers requires the 'fastapi' extra: "
        "pip install 'cubiczan-resilience[fastapi]'"
    ) from exc


def require_auth(
    *,
    env_var: str = "API_TOKEN",
    scheme_name: str = "BearerAuth",
) -> Callable[..., Any]:
    """Build a FastAPI dependency that enforces a bearer token.

    The expected token is read from ``os.environ[env_var]`` *at request time*.
    Behaviour is fail-closed:

    * env var unset or empty  -> ``503 Service Unavailable`` (server misconfig)
    * missing / malformed auth -> ``401 Unauthorized``
    * token mismatch           -> ``401 Unauthorized``

    Comparison uses :func:`hmac.compare_digest` to avoid timing leaks.
    """
    bearer = HTTPBearer(auto_error=False, scheme_name=scheme_name)

    def dependency(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    ) -> str:
        expected = os.environ.get(env_var) or ""
        if not expected:
            # Fail closed: never accept requests when no secret is configured.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authentication is not configured",
            )
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not hmac.compare_digest(credentials.credentials, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credentials.credentials

    return dependency


def cors_allowlist(
    origins: Sequence[str],
    *,
    allow_credentials: bool = True,
    allow_methods: Sequence[str] | None = None,
    allow_headers: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return safe ``CORSMiddleware`` kwargs from an explicit origin allowlist.

    Refuses the browser-unsafe combination of a wildcard origin (``"*"``) with
    ``allow_credentials=True`` by raising :class:`ValueError`. Usage::

        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, **cors_allowlist(["https://app.example.com"]))
    """
    origin_list = list(origins)
    if allow_credentials and "*" in origin_list:
        raise ValueError(
            "refusing wildcard origin '*' with allow_credentials=True; "
            "list explicit origins instead"
        )
    return {
        "allow_origins": origin_list,
        "allow_credentials": allow_credentials,
        "allow_methods": list(allow_methods) if allow_methods is not None else ["*"],
        "allow_headers": list(allow_headers) if allow_headers is not None else ["*"],
    }


__all__ = ["require_auth", "cors_allowlist"]
