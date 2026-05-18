"""Shared HTTP API authentication and per-IP rate limiting middleware.

Safe defaults:
- Authentication is enabled by default and expects a shared API key.
- If auth is enabled but no key is configured, requests fail closed (503).
- Rate limiting is enabled by default with a conservative per-IP window.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
import os

from fastapi.responses import JSONResponse
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    return max(minimum, value)


@dataclass(frozen=True)
class ApiProtectionSettings:
    auth_enabled: bool
    auth_key: str
    auth_header: str
    rate_limit_enabled: bool
    rate_limit_requests: int
    rate_limit_window_seconds: int
    trust_x_forwarded_for: bool
    exempt_paths: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "ApiProtectionSettings":
        raw_exempt_paths = os.getenv("DEEP_THINK_API_EXEMPT_PATHS", "/health,/mcp")
        exempt_paths = tuple(p.strip() for p in raw_exempt_paths.split(",") if p.strip())
        return cls(
            auth_enabled=_env_bool("DEEP_THINK_API_AUTH_ENABLED", True),
            auth_key=os.getenv("DEEP_THINK_API_KEY", ""),
            auth_header=os.getenv("DEEP_THINK_API_AUTH_HEADER", "X-API-Key"),
            rate_limit_enabled=_env_bool("DEEP_THINK_API_RATE_LIMIT_ENABLED", True),
            rate_limit_requests=_env_int("DEEP_THINK_API_RATE_LIMIT_REQUESTS", 60),
            rate_limit_window_seconds=_env_int("DEEP_THINK_API_RATE_LIMIT_WINDOW_SECONDS", 60),
            trust_x_forwarded_for=_env_bool("DEEP_THINK_API_TRUST_X_FORWARDED_FOR", True),
            exempt_paths=exempt_paths,
        )

    def is_exempt_path(self, path: str) -> bool:
        # Exact match or subtree match (e.g., /mcp, /mcp/, /mcp/messages)
        return any(path == p or path.startswith(f"{p}/") for p in self.exempt_paths)


class _SlidingWindowIpRateLimiter:
    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, client_ip: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        now = time.time()
        cutoff = now - float(window_seconds)
        with self._lock:
            q = self._requests[client_ip]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= max_requests:
                retry_after = max(1, int(q[0] + window_seconds - now))
                return False, retry_after
            q.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()


_RATE_LIMITER = _SlidingWindowIpRateLimiter()


def reset_rate_limiter_for_tests() -> None:
    _RATE_LIMITER.reset()


def _extract_client_ip(request: Request, trust_x_forwarded_for: bool) -> str:
    if trust_x_forwarded_for:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            client = xff.split(",", 1)[0].strip()
            if client:
                return client
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _is_authorized(request: Request, settings: ApiProtectionSettings) -> bool:
    header_value = request.headers.get(settings.auth_header, "")
    if header_value and secrets.compare_digest(header_value, settings.auth_key):
        return True
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token and secrets.compare_digest(token, settings.auth_key):
            return True
    return False


class ApiProtectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = ApiProtectionSettings.from_env()

        if request.method == "OPTIONS" or settings.is_exempt_path(request.url.path):
            return await call_next(request)

        if settings.auth_enabled:
            if not settings.auth_key:
                return JSONResponse(
                    {"error": "API authentication is enabled but DEEP_THINK_API_KEY is not configured"},
                    status_code=503,
                )
            if not _is_authorized(request, settings):
                return JSONResponse(
                    {"error": "Unauthorized"},
                    status_code=401,
                )

        if settings.rate_limit_enabled:
            client_ip = _extract_client_ip(request, settings.trust_x_forwarded_for)
            allowed, retry_after = _RATE_LIMITER.check(
                client_ip=client_ip,
                max_requests=settings.rate_limit_requests,
                window_seconds=settings.rate_limit_window_seconds,
            )
            if not allowed:
                return JSONResponse(
                    {"error": "Rate limit exceeded", "retry_after_seconds": retry_after},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )

        return await call_next(request)


def build_fastmcp_http_middleware() -> list[Middleware]:
    return [Middleware(ApiProtectionMiddleware)]
