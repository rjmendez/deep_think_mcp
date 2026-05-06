"""Core deep-think reasoning endpoints and creative reasoning."""

import json
import logging
import os
from datetime import datetime
from typing import Optional

from .. import store, discover as _discover
from ..engine import (
    build_provider_config,
    TASK_CLASS_PROFILES,
    classify_task,
    model_summary,
    PERSPECTIVE_MANDATES,
    CREATIVE_MODES,
    get_metrics_snapshot,
)
from ..engine.directives import resolve_skill_selection
from ..engine.validator import validate_passes, validate_width, validate_height, ValidationError

log = logging.getLogger(__name__)


def register(mcp):
    """Register reasoning routes."""
    
    @mcp.tool()
    async def deep_think_async(
        question: str,
        passes: Optional[int] = None,
        task_class: Optional[str] = None,
        skill: Optional[str] = None,
        data_policy: Optional[str] = None,
        model: Optional[str] = None,
        provider_config: Optional[dict] = None,
        verify: bool = False,
        width: Optional[int] = None,
        height: Optional[int] = None,
        extract_claims: bool = False,
        enable_research: bool = True,
        research_query: Optional[str] = None,
        dama_node_id: Optional[str] = None,
        dama_metric: Optional[str] = None,
        web_domain_whitelist: Optional[list] = None,
    ) -> dict:
        """Queue a multi-pass reasoning job and return a job_id immediately.

        The job runs asynchronously. Poll with get_thinking_result(job_id).

        Args:
            question:    The question or problem to reason about.
            passes:      Number of reasoning passes (2–6). Default 3.
            task_class:  Routing hint — picks specialist models and pass directives.
                "general"       Default reasoning (no routing). Safe default.
                "auto"          Run a lightweight classifier; apply result if confidence >= 0.75.
                "code_review"   Bug detection, security review. Uses qwen2.5-coder / gpt-5.2-codex.
                "investigation" Evidence weighing, IOC triage, incident response.
                "safety"        Risk detection, harm mapping. Runs granite3-guardian pre-check.
                "extraction"    Structured JSON output, entity extraction.
                "synthesis"     Writing, summarization, report drafting.
                "reasoning"     Complex logical / mathematical reasoning.
                "adversarial"   Unconstrained challenge reasoning. Ollama-only, NO research tools.
                "research"      Grounded research. Full research tools, no abliteration models.
            skill:          Optional predefined skill profile ID loaded from skills/*.yaml.
                            When provided, it overrides task_class routing.
            data_policy: Controls which providers are allowed.
                "any"    (default) Use any configured provider including cloud.
                "local"  Ollama ONLY — never send data to cloud providers.
                "cloud"  Cloud providers preferred; Ollama only for light tier.
            model:           Override all tiers with a single model ID (shorthand).
            provider_config: Optional per-call overrides (no secrets — use env vars for those):
                provider        "anthropic" | "copilot" | "ollama"
                base_url        Ollama endpoint, e.g. "http://localhost:11434"
                model           Single model ID for all tiers
                light           Light-tier model ID override
                medium          Medium-tier model ID override
                heavy           Heavy-tier model ID override
                light_provider  Per-tier provider (e.g. "ollama" for cheap local)
                medium_provider Per-tier provider
                heavy_provider  Per-tier provider (e.g. "copilot" for synthesis)
                temperature     Sampling temperature for supported providers
                top_p/top_k     Sampling controls for supported providers
                max_tokens      Output cap (mapped to num_predict for Ollama)
                seed            Deterministic seed for Ollama
                custom_params   Nested provider-specific sampling params
                options         Ollama-native options object
            verify:      If True, runs an extra heavy-tier re-traversal pass after the main passes
                         to check for gaps, contradictions, and unsupported claims (RYS verification).
            enable_research: If True and task_class permits, inject grounded research context.
            research_query:  Override query for research tools (defaults to question).
            dama_node_id:    DAMA device node ID for telemetry lookup (research/research_synthesis only).
            dama_metric:     DAMA metric name for telemetry lookup.
            web_domain_whitelist: Restrict web_search to specific domains (research only).

        Provider secrets via environment variables only:
            ANTHROPIC_API_KEY              Anthropic API key
            GITHUB_COPILOT_OAUTH_TOKEN     GitHub Copilot OAuth token
            OLLAMA_BASE_URL                Ollama base URL (default: http://localhost:11434)

        Response includes proof_chain field when research tools were used.
        """
        # Validate parameters (Tier 1: prevent FastMCP slice object bugs)
        try:
            passes = validate_passes(passes)
            width = validate_width(width)
            height = validate_height(height)
        except ValidationError as e:
            return {"error": str(e), "status": "validation_error"}
        
        pc: dict = dict(provider_config or {})
        if model:
            pc.setdefault("model", model)
        if data_policy and data_policy != "any":
            pc["data_policy"] = data_policy

        fan_out_enabled = width > 1
        if fan_out_enabled:
            total_passes = width * height + 1
        else:
            total_passes = max(2, min(passes, 6))

        requested_selection = skill or task_class
        if not skill and task_class == "auto":
            requested_selection = await classify_task(question, provider=pc.get("provider", ""))

        cfg = build_provider_config(pc)
        selected_skill, skill_profile = resolve_skill_selection(requested_selection)
        resolved_class = skill_profile.get("task_class", selected_skill)
        summary = model_summary(cfg, selected_skill)

        job_id = store.create_job(
            question=question,
            passes=total_passes,
            provider=cfg.provider,
            model_summary=summary,
            provider_config_json=json.dumps({
                **pc,
                "task_class": selected_skill,
                "skill": selected_skill,
                "base_task_class": resolved_class,
                "skill_version": skill_profile.get("version", 1),
                "data_policy": data_policy,
                "verify": verify,
                "enable_research": enable_research,
                "research_query": research_query,
                "dama_node_id": dama_node_id,
                "dama_metric": dama_metric,
                "web_domain_whitelist": web_domain_whitelist or [],
                "fan_out": fan_out_enabled,
                "width": width if fan_out_enabled else 1,
                "height": height if fan_out_enabled else 1,
                "extract_claims": extract_claims,
            }),
        )

        response = {
            "job_id": job_id,
            "status": "queued",
            "task_class": resolved_class,
            "skill": selected_skill,
            "skill_version": skill_profile.get("version", 1),
            "data_policy": data_policy,
            "provider": cfg.provider,
            "model_summary": summary,
            "research_enabled": enable_research and not skill_profile.get("block_research_tools", False),
        }
        
        if fan_out_enabled:
            response["fan_out"] = True
            response["width"] = width
            response["height"] = height
            response["message"] = f"Fan-out job with {width} perspectives × {height} passes. Call get_thinking_result('{job_id}') to poll."
        else:
            response["message"] = f"Call get_thinking_result('{job_id}') to poll for results."
        
        return response

    @mcp.tool()
    async def get_thinking_result(job_id: str, include_reasoning_chain: bool = False) -> dict:
        """Poll a deep_think job for results.

        Returns status (queued → running → complete | failed),
        duration_secs once complete, and the full reasoning chain + final_answer.

        For fan_out jobs, also surfaces confidence_score, converged_claims,
        contested_areas, and claim_sets at the top level for easy inspection.

        Args:
            job_id:                  The job_id returned by deep_think_async or deep_think_fan_out.
            include_reasoning_chain: If True, attach the full intermediate pass outputs from
                                     pass_cache as a "reasoning_chain" field — one entry per
                                     perspective (or "main" for standard jobs), each with an
                                     ordered list of passes (framing, tier, model, provider,
                                     output). Useful for forensic review and debugging.
                                     Default False to keep normal poll responses compact.
        """
        job = store.get_job(job_id)
        if not job:
            return {"error": f"No job found with job_id={job_id!r}"}

        duration = None
        if job.get("completed_at") and job.get("created_at"):
            try:
                start = datetime.fromisoformat(job["created_at"])
                end = datetime.fromisoformat(job["completed_at"])
                duration = int((end - start).total_seconds())
            except Exception:
                pass

        response: dict = {
            "job_id":        job["job_id"],
            "status":        job["status"],
            "provider":      job.get("provider"),
            "model_summary": job.get("model_summary"),
            "created_at":    job.get("created_at"),
            "duration_secs": duration,
        }

        if job["status"] == "complete" and job.get("result"):
            try:
                result = json.loads(job["result"])
                response["result"] = result
                if isinstance(result, dict) and result.get("type") == "fan_out":
                    if result.get("confidence_score") is not None:
                        response["confidence_score"] = result["confidence_score"]
                    if result.get("converged_claims"):
                        response["converged_claims"] = result["converged_claims"]
                    if result.get("contested_areas"):
                        response["contested_areas"] = result["contested_areas"]
                    if result.get("claim_sets"):
                        response["claim_sets"] = result["claim_sets"]
                    response["adaptive_triggered"] = result.get("adaptive_triggered", False)
                    if result.get("adaptive_triggered"):
                        response["adaptive_reason"] = result.get("adaptive_reason", "")
                        response["final_width"] = result.get("final_width")
                if isinstance(result, dict) and result.get("verification_pass") is not None:
                    response["verification_pass"] = result["verification_pass"]
                if isinstance(result, dict):
                    if result.get("verification_status") is not None:
                        response["verification_status"] = result["verification_status"]
                    if result.get("verification_results") is not None:
                        response["verification_results"] = result["verification_results"]
                    if result.get("adjusted_final_confidence") is not None:
                        response["adjusted_final_confidence"] = result["adjusted_final_confidence"]
                    if result.get("verification_summary") is not None:
                        response["verification_summary"] = result["verification_summary"]
                    if result.get("escalated_claim_ids"):
                        response["escalated_claim_ids"] = result["escalated_claim_ids"]
            except (json.JSONDecodeError, TypeError):
                response["result"] = job["result"]
        elif job["status"] == "failed":
            response["error"] = job.get("error")

        if include_reasoning_chain:
            response["reasoning_chain"] = store.get_full_reasoning_chain(job_id)

        return response

    @mcp.tool()
    async def discover_models(force: bool = False, benchmark: bool = True) -> dict:
        """Discover available models, benchmark their latency, and assign tiers.

        Runs automatically at server startup (non-blocking). Call this tool to:
          - See what models are available and how they were assigned to tiers
          - Force a re-benchmark after adding/removing Ollama models
          - Check benchmarked timeouts (conservative: benchmark × 8, min 45s, max 300s)

        Args:
            force:     Re-run even if the cache is fresh (< 24h old). Default False.
            benchmark: Actually measure latency per model. Set False for a fast inventory
                       without benchmarking (uses size-based heuristics only). Default True.

        Returns a summary of discovered models grouped by provider and tier.
        """
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        result = await _discover.run_discovery(base_url, force=force, benchmark=benchmark)

        by_provider: dict[str, list[dict]] = {}
        for m in result.models:
            entry = {
                "model_id":       m.model_id,
                "tier":           m.suggested_tier,
                "capabilities":   m.capabilities,
                "benchmark_ms":   m.benchmark_ms if m.benchmark_ms else "not measured",
                "timeout_secs":   m.timeout_secs,
            }
            if m.benchmark_ms:
                entry["size_b"] = m.size_b
            by_provider.setdefault(m.provider, []).append(entry)

        tier_summary = {
            provider: {"light": ta.light, "medium": ta.medium, "heavy": ta.heavy}
            for provider, ta in result.tier_assignments.items()
        }

        return {
            "from_cache":       result.from_cache,
            "completed_at":     result.completed_at,
            "discovery_secs":   round(result.discovery_secs, 1),
            "errors":           result.errors,
            "tier_assignments": tier_summary,
            "models":           by_provider,
        }

    @mcp.tool()
    async def deep_think_fan_out(
        question: str,
        width: Optional[int] = None,
        height: Optional[int] = None,
        task_class: Optional[str] = None,
        skill: Optional[str] = None,
        data_policy: Optional[str] = None,
        max_parallel: Optional[int] = None,
        max_width: Optional[int] = None,
        confidence_threshold: Optional[int] = None,
        extract_claims: bool = False,
        provider_config: Optional[dict] = None,
    ) -> dict:
        """Queue a perspective fan-out reasoning job and return a job_id immediately.

        Runs `width` parallel agents each with a structurally different mandate (e.g. defense /
        prosecution / forensics), each doing `height` sequential reasoning passes. A final
        synthesis pass integrates all perspectives, identifying convergence (high confidence)
        and divergence (contested areas).

        If synthesis confidence_score < confidence_threshold (default 50) OR contested_areas > 2,
        automatically dispatches remaining unused perspective mandates and re-synthesizes with all
        outputs (adaptive width expansion — DAMA sampling_factor analog). Limited to 1 expansion
        to cap API spend.

        Poll with get_thinking_result(job_id) until status is "complete".

        Args:
            question:             The question or content to analyze.
            width:                Parallel perspectives (1–6). Task class determines which mandates are used.
                                  width=3 → first 3 mandates; width=6 → all 6.
            height:               Sequential passes per perspective (1–5). Each perspective runs this many
                                  reasoning passes with its mandate injected into every prompt.
            task_class:           Selects the mandate set and specialist models.
                "investigation" → defense / prosecution / forensics / compliance / red_team / timeline
                "general"       → primary / adversarial / alternative / technical / risk / devils_advocate
                "code_review"   → correctness / security / performance / maintainability / api_contract / edge_cases
                "safety"        → harm_assessment / policy_compliance / mitigations / false_positives / context / legal
                "reasoning"     → formal / adversarial / constraints / alternative / verification / simplification
                "synthesis"     → structure / accuracy / clarity / completeness / audience / attribution
                "extraction"    → schema / completeness / disambiguation / confidence / validation / context
            skill:               Optional predefined skill profile ID loaded from skills/*.yaml.
                                  When provided, it overrides task_class routing.
            data_policy:          "any" | "local" | "cloud"
            max_parallel:         Max concurrent perspectives (default 2 — safe for Copilot Business
                                  heavy-tier limits). Increase to 4 for Enterprise accounts.
            max_width:            Upper bound on total perspectives after adaptive expansion (default 6).
            confidence_threshold: Trigger adaptive expansion when confidence_score < this value (default 50).
            extract_claims:       If True, distil each perspective's prose into a structured claim set
                                  (light-tier model) before synthesis. Reduces synthesis context ~10-20×.
                                  Default False.
            provider_config: Optional per-call model/provider overrides (no secrets).

        Total LLM calls = (width × height) + 1 synthesis pass (+ adaptive expansion if triggered).
        Example: width=3, height=2 → 7 total calls (6 perspective passes + 1 synthesis).
        """
        # Apply defaults
        width = width if width is not None else 3
        height = height if height is not None else 2
        task_class = task_class if task_class is not None else "general"
        data_policy = data_policy if data_policy is not None else "any"
        
        # Validate parameters (Tier 1: prevent FastMCP slice object bugs)
        try:
            width = validate_width(width)
            height = validate_height(height)
        except ValidationError as e:
            return {"error": str(e), "status": "validation_error"}
        
        max_parallel = max_parallel if max_parallel is not None else 2
        max_width = max_width if max_width is not None else 6
        confidence_threshold = confidence_threshold if confidence_threshold is not None else 50
        
        pc: dict = dict(provider_config or {})
        if data_policy and data_policy != "any":
            pc["data_policy"] = data_policy

        requested_selection = skill or task_class
        if not skill and task_class == "auto":
            requested_selection = await classify_task(question, provider=pc.get("provider", ""))

        cfg = build_provider_config(pc)
        selected_skill, skill_profile = resolve_skill_selection(requested_selection)
        resolved_class = skill_profile.get("task_class", selected_skill)
        summary = model_summary(cfg, selected_skill)

        width = max(1, min(width, 6))
        height = max(1, min(height, 5))
        total_calls = width * height + 1

        job_id = store.create_job(
            question=question,
            passes=total_calls,
            provider=cfg.provider,
            model_summary=summary,
            provider_config_json=json.dumps({
                **pc,
                "task_class": selected_skill,
                "skill": selected_skill,
                "base_task_class": resolved_class,
                "skill_version": skill_profile.get("version", 1),
                "data_policy": data_policy,
                "fan_out": True,
                "width": width,
                "height": height,
                "max_parallel": max_parallel,
                "max_width": max_width,
                "confidence_threshold": confidence_threshold,
                "extract_claims": extract_claims,
            }),
        )

        mandates = PERSPECTIVE_MANDATES.get(selected_skill, PERSPECTIVE_MANDATES["general"])
        # Extract first 'width' perspective names from the mandates dict
        perspective_names = list(mandates.keys())[:width]

        return {
            "job_id": job_id,
            "status": "queued",
            "task_class": resolved_class,
            "skill": selected_skill,
            "skill_version": skill_profile.get("version", 1),
            "width": width,
            "height": height,
            "total_llm_calls": total_calls,
            "perspectives": perspective_names,
            "data_policy": data_policy,
            "provider": cfg.provider,
            "model_summary": summary,
            "message": f"Call get_thinking_result('{job_id}') to poll for results.",
        }

    @mcp.tool()
    async def list_thinking_jobs(status: str = "all", limit: int = 10) -> dict:
        """List recent thinking jobs.

        Args:
            status: Filter by status — "all", "queued", "running", "complete", "failed".
            limit:  Max number of jobs to return (max 100).
        """
        jobs = store.list_jobs(status=status, limit=min(limit, 100))
        return {"count": len(jobs), "jobs": jobs}

    @mcp.tool()
    async def deep_think_creative(
        question: str,
        mode: str = "lateral-thinking",
        passes: int = 4,
        data_policy: str = "any",
        model: str = "",
        provider_config: Optional[dict] = None,
        verify_with_nova: bool = False,
    ) -> dict:
        """Queue a high-temperature creative reasoning job and return a job_id immediately.

        Runs multi-pass creative reasoning with dynamic temperature scheduling and
        mode-specific prompt templates that evolve progressively across passes.

        Poll with get_thinking_result(job_id) to check status and retrieve results.

        Args:
            question:         The problem or question to explore creatively.
            mode:             Creative reasoning mode:
                "lateral-thinking"  Sideways problem solving, constraint-violation exploration.
                "blue-sky"          Unconstrained ideation, "what if" scenarios.
                "socratic"          Questioning assumptions, dialectical exploration.
                "evolutionary"      Iterative idea building; temperature decreases across passes.
            passes:           Number of reasoning passes (2–6, default 4).
            data_policy:      "any" | "local" | "cloud"
            model:            Override all tiers with a single model ID (shorthand).
            provider_config:  Optional per-call overrides (no secrets — use env vars for those).
            verify_with_nova: If True, verify the best-scoring pass against Nova's /pre_action.

        Temperature schedule (automatic):
            Passes 1-2:  0.8–1.0  (high exploration)
            Passes 3-4:  0.6–0.7  (medium refinement)
            Final pass:  0.3–0.5  (validation / convergence)
            Dynamic adjustment ±0.05 based on novelty score feedback.

        Quality metrics returned per pass:
            novelty_score    (0-1): divergence from conventional reasoning
            feasibility_score (0-1): implementability / realism
            impact_score      (0-1): potential significance
            combined_score    = novelty × feasibility × impact
        """
        if mode not in CREATIVE_MODES:
            return {
                "error": f"Unknown creative mode '{mode}'. Valid modes: {list(CREATIVE_MODES)}",
            }

        pc: dict = dict(provider_config or {})
        if model:
            pc.setdefault("model", model)
        if data_policy and data_policy != "any":
            pc["data_policy"] = data_policy

        cfg = build_provider_config(pc)
        summary = model_summary(cfg, "general")
        passes = max(2, min(passes, 6))

        job_id = store.create_job(
            question=question,
            passes=passes,
            provider=cfg.provider,
            model_summary=summary,
            provider_config_json=json.dumps({
                **pc,
                "creative":        True,
                "creative_mode":   mode,
                "creative_passes": passes,
                "data_policy":     data_policy,
                "verify_with_nova": verify_with_nova,
            }),
        )

        return {
            "job_id":        job_id,
            "status":        "queued",
            "mode":          mode,
            "passes":        passes,
            "data_policy":   data_policy,
            "provider":      cfg.provider,
            "model_summary": summary,
            "temperature_schedule": {
                "passes_1_2": "0.8–1.0 (high exploration)",
                "passes_3_4": "0.6–0.7 (medium refinement)",
                "final_pass": "0.3–0.5 (validation)",
            },
            "message": f"Call get_thinking_result('{job_id}') to poll for results.",
        }

    @mcp.tool()
    async def get_creative_metrics() -> dict:
        """Return accumulated creativity metrics for trend analysis.

        Metrics are tracked in-process across all creative reasoning jobs run
        in this server session. Useful for understanding which modes and ideas
        tend to score highest on novelty, feasibility, and impact.

        Returns:
            total_jobs, total_passes, verified_passes, rolling averages per dimension,
            per-mode job counts, and per-mode average combined scores.
        """
        return get_metrics_snapshot()
