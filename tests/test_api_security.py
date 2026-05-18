import importlib

from fastapi.testclient import TestClient

from api_security import reset_rate_limiter_for_tests


def _set_protection_env(monkeypatch, *, auth_key: str = "test-key", rate_limit_requests: int = 60):
    monkeypatch.setenv("DEEP_THINK_API_AUTH_ENABLED", "1")
    monkeypatch.setenv("DEEP_THINK_API_KEY", auth_key)
    monkeypatch.setenv("DEEP_THINK_API_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("DEEP_THINK_API_RATE_LIMIT_REQUESTS", str(rate_limit_requests))
    monkeypatch.setenv("DEEP_THINK_API_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("DEEP_THINK_API_EXEMPT_PATHS", "")
    reset_rate_limiter_for_tests()


def test_http_api_requires_auth_for_protected_endpoint(monkeypatch):
    _set_protection_env(monkeypatch, auth_key="secret", rate_limit_requests=100)
    http_api = importlib.import_module("http_api")
    monkeypatch.setattr(http_api, "list_jobs", lambda status=None, limit=10: [])
    client = TestClient(http_api.app)

    unauthorized = client.get("/api/v1/jobs")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"] == "Unauthorized"

    authorized = client.get("/api/v1/jobs", headers={"X-API-Key": "secret"})
    assert authorized.status_code == 200
    assert authorized.json()["count"] == 0


def test_http_api_enforces_per_ip_rate_limit(monkeypatch):
    _set_protection_env(monkeypatch, auth_key="secret", rate_limit_requests=2)
    http_api = importlib.import_module("http_api")
    monkeypatch.setattr(http_api, "list_jobs", lambda status=None, limit=10: [])
    client = TestClient(http_api.app)
    headers = {"X-API-Key": "secret"}

    assert client.get("/api/v1/jobs", headers=headers).status_code == 200
    assert client.get("/api/v1/jobs", headers=headers).status_code == 200

    limited = client.get("/api/v1/jobs", headers=headers)
    assert limited.status_code == 429
    assert limited.json()["error"] == "Rate limit exceeded"
    assert "Retry-After" in limited.headers


def test_server_streamable_http_uses_protection_middleware(monkeypatch):
    _set_protection_env(monkeypatch)
    from deep_think_mcp import server

    captured: dict = {}

    class DummyMCP:
        def http_app(self, **kwargs):
            captured["http_app_kwargs"] = kwargs
            return "dummy-app"

        def run(self, **kwargs):
            captured["run_kwargs"] = kwargs

    monkeypatch.setattr(server, "mcp", DummyMCP())
    monkeypatch.setattr(server, "uvicorn_run", lambda app, **kwargs: captured.update({"uvicorn_app": app, "uvicorn_kwargs": kwargs}))
    monkeypatch.setenv("DEEP_THINK_TRANSPORT", "streamable-http")
    monkeypatch.setenv("DEEP_THINK_HOST", "127.0.0.1")
    monkeypatch.setenv("DEEP_THINK_PORT", "8123")

    server.main()

    assert captured["http_app_kwargs"]["transport"] == "streamable-http"
    middleware_stack = captured["http_app_kwargs"]["middleware"]
    assert middleware_stack and middleware_stack[0].cls.__name__ == "ApiProtectionMiddleware"
    assert captured["uvicorn_app"] == "dummy-app"
