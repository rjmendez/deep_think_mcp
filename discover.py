"""Model discovery and capability benchmarking for deep_think_mcp.

Runs at startup to:
  1. Discover available Ollama models via /api/tags
  2. Benchmark each model with a representative prompt (measures realistic latency)
  3. Assign light/medium/heavy tiers based on size + observed latency
  4. Set conservative per-model timeouts (benchmark × 8, clamped 45s–300s)
  5. Detect configured cloud providers and assign fixed conservative timeouts
  6. Cache everything in SQLite (24h TTL, invalidated when Ollama model set changes)

The result is a DiscoveryResult that engine.py consults for tier assignment
and per-call timeouts instead of hardcoded defaults.

Discovery runs non-blocking at startup — the server is immediately usable.
Jobs submitted before discovery completes use conservative fallback timeouts.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Benchmark prompt chosen to produce ~100-200 tokens: realistic for a light pass,
# long enough to capture actual generation throughput (not just TTFT).
_BENCHMARK_PROMPT = (
    "List exactly 5 key principles of good software architecture. "
    "Be concise — one sentence per principle. Number each one."
)
_BENCHMARK_TIMEOUT = 90.0   # per model; generous for large local models
_BENCHMARK_MAX_TOKENS = 300

# How long a cached discovery result is considered fresh
_CACHE_MAX_AGE_HOURS = 24

# Timeout multiplier applied to benchmark latency to get a conservative call timeout.
# 8× accounts for: longer prompts, longer outputs, variable GPU load, queue depth.
_TIMEOUT_MULTIPLIER = 8.0
_TIMEOUT_MIN_SECS = 45
_TIMEOUT_MAX_SECS = 300

# Conservative timeouts for cloud models — not benchmarked (costs money / rate limits).
# Keyed on model ID substring (longest match wins).
_CLOUD_TIMEOUTS: list[tuple[str, int]] = [
    # Fast / mini models
    ("gpt-4o-mini",    45),
    ("haiku",          45),
    ("gpt-5-mini",     45),
    ("gpt-4.1",        60),
    # Sonnet / mid-range
    ("sonnet-4.5",     90),
    ("sonnet-4.6",     90),
    ("sonnet-4",       90),
    ("gpt-5.2-codex",  90),
    ("gpt-5.3-codex",  90),
    ("gpt-5.2",        90),
    ("gpt-5.3",        90),
    # Opus / large
    ("opus-4.5",      180),
    ("opus-4.6",      180),
    ("opus-4.7",      180),
    ("opus-4",        180),
    # Abliteration default cloud model
    ("abliterated-model", 120),
    ("gpt-5.4",       120),
]
_CLOUD_TIMEOUT_DEFAULT = 120


def cloud_timeout(model_id: str) -> int:
    """Return conservative timeout in seconds for a cloud model."""
    m = model_id.lower()
    # Longest matching substring wins
    best = ("", _CLOUD_TIMEOUT_DEFAULT)
    for fragment, secs in _CLOUD_TIMEOUTS:
        if fragment in m and len(fragment) > len(best[0]):
            best = (fragment, secs)
    return best[1]


# ---------------------------------------------------------------------------
# Model classification heuristics
# ---------------------------------------------------------------------------

def _parse_size_b(model_id: str) -> float:
    """Estimate parameter count (billions) from model ID string."""
    m = re.search(r"[:\-_](\d+\.?\d*)b\b", model_id.lower())
    if m:
        return float(m.group(1))
    # Known models without explicit size tag
    known = {
        "phi4-mini":   3.8,
        "mistral":     7.0,
        "granite3-guardian": 2.0,
    }
    lid = model_id.lower()
    for key, size in known.items():
        if key in lid:
            return size
    return 0.0


def _is_embedding_only(model_id: str) -> bool:
    return any(x in model_id.lower() for x in ("embed", "nomic", "bge-m3", "mxbai"))


def _capabilities(model_id: str) -> list[str]:
    """Infer capability tags from model name."""
    lid = model_id.lower()
    if "guardian" in lid:
        return ["safety"]           # binary classifier only — not a reasoning model
    caps = ["general"]
    if any(x in lid for x in ("coder", "-code", "codex", "deepseek-coder", "qwen3-coder")):
        caps.append("code")
    if any(x in lid for x in ("r1", "thinking", "qwq", "deepseek-r1")):
        caps.append("reasoning")
    return caps


def _suggest_tier(size_b: float, benchmark_ms: int) -> str:
    """Map size + observed latency to a tier suggestion."""
    # Latency takes priority — a fast large model is still usable as light
    if benchmark_ms > 0:
        if benchmark_ms < 8_000:
            return "light"
        if benchmark_ms < 40_000:
            return "medium"
        return "heavy"
    # Fall back to size estimate
    if size_b <= 0:
        return "medium"
    if size_b <= 4.5:
        return "light"
    if size_b <= 14.0:
        return "medium"
    return "heavy"


def _timeout_from_benchmark(benchmark_ms: int) -> int:
    """Calculate conservative timeout from benchmark latency."""
    if benchmark_ms <= 0:
        return _TIMEOUT_MAX_SECS
    secs = (benchmark_ms / 1000.0) * _TIMEOUT_MULTIPLIER
    return int(max(_TIMEOUT_MIN_SECS, min(_TIMEOUT_MAX_SECS, secs)))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ModelInfo:
    model_id: str
    provider: str                          # "ollama" | "anthropic" | "copilot" | "abliteration"
    size_b: float = 0.0
    suggested_tier: str = "medium"
    capabilities: list = field(default_factory=list)
    benchmark_ms: int = 0                  # wall-clock ms for benchmark prompt; 0 = not measured
    timeout_secs: int = _TIMEOUT_MAX_SECS  # conservative per-call timeout
    is_available: bool = True
    last_checked: str = ""


@dataclass
class TierAssignment:
    light: str = ""
    medium: str = ""
    heavy: str = ""


@dataclass
class DiscoveryResult:
    """Holds the full output of a discovery run."""
    models: list[ModelInfo] = field(default_factory=list)
    # Per-provider tier assignments: {"ollama": TierAssignment, "copilot": TierAssignment, ...}
    tier_assignments: dict[str, TierAssignment] = field(default_factory=dict)
    from_cache: bool = False
    discovery_secs: float = 0.0
    errors: list[str] = field(default_factory=list)
    completed_at: str = ""

    def by_id(self, model_id: str, provider: str) -> ModelInfo | None:
        for m in self.models:
            if m.model_id == model_id and m.provider == provider:
                return m
        return None

    def timeout_for(self, model_id: str, provider: str) -> int:
        info = self.by_id(model_id, provider)
        if info:
            return info.timeout_secs
        if provider in ("anthropic", "copilot"):
            return cloud_timeout(model_id)
        return _TIMEOUT_MAX_SECS


# Module-level cache — populated by run_discovery(), consulted by engine.py
_current: DiscoveryResult | None = None


def get_current() -> DiscoveryResult | None:
    return _current


# ---------------------------------------------------------------------------
# Benchmarking
# ---------------------------------------------------------------------------

async def _benchmark_ollama(model_id: str, base_url: str) -> int:
    """Run benchmark prompt and return wall-clock ms. Returns 0 on failure."""
    import httpx  # type: ignore
    payload = {
        "model": model_id,
        "prompt": _BENCHMARK_PROMPT,
        "stream": False,
        "options": {"num_predict": _BENCHMARK_MAX_TOKENS},
    }
    # Disable Qwen thinking for benchmark — we want generation time, not thinking time
    if "qwen" in model_id.lower():
        payload["think"] = False
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_BENCHMARK_TIMEOUT) as client:
            resp = await client.post(f"{base_url}/api/generate", json=payload)
            resp.raise_for_status()
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.debug("Benchmark %s: %d ms", model_id, elapsed_ms)
            return elapsed_ms
    except Exception as e:
        log.warning("Benchmark failed for %s: %s", model_id, e)
        return 0


# ---------------------------------------------------------------------------
# Cloud provider detection
# ---------------------------------------------------------------------------

def _detect_cloud_providers() -> list[ModelInfo]:
    """Return ModelInfo stubs for configured cloud providers."""
    models: list[ModelInfo] = []
    now = datetime.now(timezone.utc).isoformat()

    # Anthropic
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if anthropic_key and anthropic_key not in ("not-set", ""):
        for mid, tier in [
            ("claude-haiku-4-5",  "light"),
            ("claude-sonnet-4-6", "medium"),
            ("claude-opus-4-7",   "heavy"),
        ]:
            models.append(ModelInfo(
                model_id=mid, provider="anthropic",
                suggested_tier=tier, capabilities=["general", "code", "reasoning"],
                timeout_secs=cloud_timeout(mid), last_checked=now,
            ))

    # GitHub Copilot
    copilot_token = ""
    env_token = os.getenv("GITHUB_COPILOT_OAUTH_TOKEN", "").strip()
    if env_token:
        copilot_token = env_token
    else:
        try:
            import yaml  # type: ignore
            hosts_path = os.path.expanduser("~/.config/gh/hosts.yml")
            with open(hosts_path) as f:
                hosts = yaml.safe_load(f) or {}
            copilot_token = hosts.get("github.com", {}).get("oauth_token", "")
        except Exception:
            pass

    if copilot_token:
        for mid, tier in [
            ("gpt-4.1",            "light"),
            ("gpt-5.4",            "medium"),
            ("gpt-5.5",            "heavy"),
            ("gpt-5.2-codex",      "medium"),  # code specialist
        ]:
            models.append(ModelInfo(
                model_id=mid, provider="copilot",
                suggested_tier=tier, capabilities=["general", "code", "reasoning"],
                timeout_secs=cloud_timeout(mid), last_checked=now,
            ))

    # Abliteration
    abliteration_key = os.getenv("ABLITERATION_API_KEY", "").strip()
    if not abliteration_key:
        try:
            import socket

            hostname = socket.gethostname()
            cred_path = os.path.expanduser("~/.abliteration/credentials")
            if os.path.exists(cred_path):
                with open(cred_path, encoding="utf-8") as cred_file:
                    for line in cred_file:
                        if line.startswith(f"{hostname}="):
                            abliteration_key = line.split("=", 1)[1].strip()
                            break
        except Exception:
            pass

    if abliteration_key:
        mid = "abliterated-model"
        models.append(ModelInfo(
            model_id=mid, provider="abliteration",
            suggested_tier="medium", capabilities=["general", "code", "reasoning"],
            timeout_secs=cloud_timeout(mid), last_checked=now,
        ))

    return models


# ---------------------------------------------------------------------------
# Tier assignment from a list of available models
# ---------------------------------------------------------------------------

def _assign_tiers(models: list[ModelInfo]) -> dict[str, TierAssignment]:
    """Given discovered models, compute optimal tier assignments per provider."""
    by_provider: dict[str, list[ModelInfo]] = {}
    for m in models:
        if not m.is_available:
            continue
        if _is_embedding_only(m.model_id):
            continue
        if "safety" in m.capabilities and m.capabilities == ["safety"]:
            continue  # guardian is not a general reasoning model
        by_provider.setdefault(m.provider, []).append(m)

    result: dict[str, TierAssignment] = {}

    for provider, available in by_provider.items():
        # Sort by: tier preference (light < medium < heavy), then size
        _tier_order = {"light": 0, "medium": 1, "heavy": 2}
        available.sort(key=lambda m: (_tier_order.get(m.suggested_tier, 1), m.size_b))

        lights  = [m for m in available if m.suggested_tier == "light"]
        mediums = [m for m in available if m.suggested_tier == "medium"]
        heavies = [m for m in available if m.suggested_tier == "heavy"]

        # Fallback: if a tier is empty, use the nearest tier
        all_sorted = sorted(available, key=lambda m: m.size_b)

        light_model  = (lights[0]  if lights  else all_sorted[0]               if all_sorted else None)
        heavy_model  = (heavies[0] if heavies else all_sorted[-1]              if all_sorted else None)
        medium_model = (mediums[0] if mediums else
                        (all_sorted[len(all_sorted)//2] if len(all_sorted) > 1 else heavy_model))

        result[provider] = TierAssignment(
            light  = light_model.model_id  if light_model  else "",
            medium = medium_model.model_id if medium_model else "",
            heavy  = heavy_model.model_id  if heavy_model  else "",
        )

    return result


# ---------------------------------------------------------------------------
# Persistence helpers (via store module)
# ---------------------------------------------------------------------------

def _save_to_store(result: DiscoveryResult, ollama_model_hash: str) -> None:
    from . import store  # avoid circular at module load
    store.save_discovery(result, ollama_model_hash)


def _load_from_store(current_hash: str) -> DiscoveryResult | None:
    from . import store
    return store.load_discovery(current_hash, max_age_hours=_CACHE_MAX_AGE_HOURS)


# ---------------------------------------------------------------------------
# Main discovery entry point
# ---------------------------------------------------------------------------

async def run_discovery(
    base_url: str,
    force: bool = False,
    benchmark: bool = True,
) -> DiscoveryResult:
    """Run full model discovery and benchmarking.

    Args:
        base_url:  Ollama base URL.
        force:     Skip cache and re-run even if fresh data exists.
        benchmark: Run latency benchmarks (set False in tests / fast startup).

    Returns a DiscoveryResult. Also updates the module-level _current cache.
    """
    global _current
    t0 = time.monotonic()
    result = DiscoveryResult()

    # --- Step 1: Query Ollama ---
    import httpx  # type: ignore
    ollama_model_ids: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            ollama_model_ids = [m["name"] for m in resp.json().get("models", [])]
        log.info("Ollama discovery: %d models at %s", len(ollama_model_ids), base_url)
    except Exception as e:
        log.warning("Ollama unreachable at %s: %s", base_url, e)
        result.errors.append(f"Ollama unreachable: {e}")

    # Hash the current set of Ollama models AND the cloud model list so that
    # any code change to _detect_cloud_providers() busts the cache.
    _cloud_ids = sorted(m.model_id for m in _detect_cloud_providers())
    ollama_hash = hashlib.md5(
        json.dumps({"ollama": sorted(ollama_model_ids), "cloud": _cloud_ids}).encode()
    ).hexdigest()[:12]

    # --- Step 2: Check cache ---
    if not force:
        cached = _load_from_store(ollama_hash)
        if cached:
            cached.from_cache = True
            _current = cached
            log.info(
                "Discovery: loaded from cache (%d models, assigned at %s)",
                len(cached.models), cached.completed_at,
            )
            for provider, ta in cached.tier_assignments.items():
                if provider in ("anthropic", "copilot"):
                    log.warning(
                        "CLOUD MODELS (cache) [%s] light=%s medium=%s heavy=%s",
                        provider, ta.light, ta.medium, ta.heavy,
                    )
            return cached

    # --- Step 3: Benchmark Ollama models ---
    now = datetime.now(timezone.utc).isoformat()
    for model_id in ollama_model_ids:
        if _is_embedding_only(model_id):
            log.debug("Skipping embedding model: %s", model_id)
            continue

        size_b = _parse_size_b(model_id)
        caps = _capabilities(model_id)

        bench_ms = 0
        if benchmark and caps != ["safety"]:  # don't benchmark guardian (binary output)
            try:
                bench_ms = await asyncio.wait_for(
                    _benchmark_ollama(model_id, base_url),
                    timeout=_BENCHMARK_TIMEOUT + 5,
                )
            except asyncio.TimeoutError:
                log.warning("Benchmark timed out for %s", model_id)

        tier = _suggest_tier(size_b, bench_ms)
        timeout = _timeout_from_benchmark(bench_ms) if bench_ms > 0 else _TIMEOUT_MAX_SECS

        result.models.append(ModelInfo(
            model_id=model_id,
            provider="ollama",
            size_b=size_b,
            suggested_tier=tier,
            capabilities=caps,
            benchmark_ms=bench_ms,
            timeout_secs=timeout,
            is_available=True,
            last_checked=now,
        ))
        log.info(
            "  %-30s  size=%.1fB  bench=%dms  tier=%-6s  timeout=%ds",
            model_id, size_b, bench_ms, tier, timeout,
        )

    # --- Step 4: Detect cloud providers ---
    result.models.extend(_detect_cloud_providers())

    # --- Step 5: Assign tiers ---
    result.tier_assignments = _assign_tiers(result.models)

    result.discovery_secs = time.monotonic() - t0
    result.completed_at = datetime.now(timezone.utc).isoformat()

    log.info(
        "Discovery complete in %.1fs — tier assignments: %s",
        result.discovery_secs,
        {p: vars(ta) for p, ta in result.tier_assignments.items()},
    )

    # Explicit cloud model audit log — catch stale/date-coded IDs immediately.
    for provider, ta in result.tier_assignments.items():
        if provider in ("anthropic", "copilot"):
            log.warning(
                "CLOUD MODELS [%s] light=%s medium=%s heavy=%s",
                provider, ta.light, ta.medium, ta.heavy,
            )

    # --- Step 6: Persist ---
    try:
        _save_to_store(result, ollama_hash)
    except Exception as e:
        log.warning("Failed to save discovery cache: %s", e)

    _current = result
    return result
