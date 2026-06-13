import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cubiczan_resilience.fastapi_helpers import (  # noqa: E402
    cors_allowlist,
    require_auth,
)


def _make_app() -> FastAPI:
    app = FastAPI()
    auth = require_auth(env_var="API_TOKEN")

    @app.get("/secure")
    def secure(_: str = Depends(auth)):
        return {"ok": True}

    return app


def test_rejects_when_token_unset(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    client = TestClient(_make_app())
    r = client.get("/secure", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503  # fail-closed: no secret configured


def test_rejects_on_mismatch(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "right-token")
    client = TestClient(_make_app())
    r = client.get("/secure", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_rejects_when_missing_header(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "right-token")
    client = TestClient(_make_app())
    r = client.get("/secure")
    assert r.status_code == 401


def test_accepts_valid_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "right-token")
    client = TestClient(_make_app())
    r = client.get("/secure", headers={"Authorization": "Bearer right-token"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_cors_allowlist_rejects_wildcard_with_credentials():
    with pytest.raises(ValueError):
        cors_allowlist(["*"], allow_credentials=True)


def test_cors_allowlist_ok():
    cfg = cors_allowlist(["https://app.example.com"], allow_credentials=True)
    assert cfg["allow_origins"] == ["https://app.example.com"]
    assert cfg["allow_credentials"] is True


def test_cors_wildcard_allowed_without_credentials():
    cfg = cors_allowlist(["*"], allow_credentials=False)
    assert cfg["allow_origins"] == ["*"]
