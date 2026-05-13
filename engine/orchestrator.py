"""Main orchestration loop and fan-out reasoning implementation.

Handles:
- deep_think_passes: Main multi-pass reasoning loop
- run_fan_out: Parallel perspective reasoning with synthesis
- Utility functions for pass execution, claim extraction, validation
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, replace as dataclasses_replace
from typing import Optional, Any

try:
    from ground_truth import GroundTruthProvider, Claim
except ImportError:
    GroundTruthProvider = None
    Claim = None

from .types import ProviderConfig, PassResult, ValidationData
from . import provider as provider_module
from . import directives as directives_module
from .task_class_enforcer import (
    enforce_task_class,
    check_research_tool_allowed,
    filter_adversarial_output,
)
from .validator import validate_passes, validate_width, validate_height, ValidationError
from deep_think_mcp import store
from deep_think_mcp import discover
from deep_think_mcp.defaults import DEFAULT_TOOL_EVIDENCE_WEIGHT

log = logging.getLogger(__name__)


def _build_tool_query(question: str, perspective_answer: str, max_chars: int = 1800) -> str:
    """Build a tool query that prioritizes perspective-specific context."""
    perspective = (perspective_answer or "").strip()
    base_question = (question or "").strip()
    parts = [part for part in (perspective, base_question) if part]
    if not parts:
        return ""
    query = "\n\n".join(parts)
    return query[:max_chars]


def _log_pass_exception(
    *,
    pass_num: int,
    framing: str,
    tier: str,
    provider: str,
    model: str,
    error: str,
    exception_type: str,
    job_id: Optional[str] = None,
    task_class: Optional[str] = None,
    provider_config: Optional[dict] = None,
    **extra: Any,
) -> None:
    """Emit structured pass-level failure context without changing result shape."""
    custom_params = {}
    try:
        custom_params = provider_module._custom_params_from_provider_config(provider, provider_config)
    except Exception:
        log.debug("Failed to extract custom params for pass exception", exc_info=True)

    payload = {
        "event": "pass_exception",
        "job_id": job_id,
        "task_class": task_class,
        "pass_num": pass_num,
        "framing": framing,
        "tier": tier,
        "provider": provider,
        "model": model,
        "error": error,
        "exception_type": exception_type,
    }
    if custom_params:
        payload["custom_params"] = custom_params
    payload.update({k: v for k, v in extra.items() if v is not None})
    log.error("pass_event %s", json.dumps(payload, sort_keys=True, default=str))


# ---------------------------------------------------------------------------
# Claim extraction (from engine.py lines 1349-1429)
# ---------------------------------------------------------------------------

def _extract_claims_from_pass_output(output: str) -> list[dict]:
    """Extract structured claims from pass output using pattern matching.
    
    Returns list of claim dicts with all Claim dataclass fields:
    id, statement, claim_type, subject, expected_value, confidence_model
    """
    if GroundTruthProvider is None or Claim is None:
        return []
    
    claims = []
    claim_counter = 0
    
    # Pattern 1: "CLAIM: ... [CONFIDENCE: X%]"
    # Use lookahead to avoid stopping at brackets in the claim text
    claim_pattern = r"(?i)claim:\s*(.+?)(?=\s*\[confidence:\s*\d+%\]|$)"
    for match in re.finditer(claim_pattern, output, re.MULTILINE):
        text = match.group(1).strip()
        # Extract confidence if present
        conf_match = re.search(r"\[confidence:\s*(\d+)%\]", output[match.end():match.end()+100], re.IGNORECASE)
        conf = int(conf_match.group(1)) / 100 if conf_match else 0.5
        
        claim_data = _build_claim_data(
            statement=text,
            confidence_model=conf,
            claim_type="inferred",
            claim_id=claim_counter,
        )
        claims.append(claim_data)
        claim_counter += 1
    
    # Pattern 2: "(✓) ... [N% confidence]" or "(✗) ... [N% confidence]"
    # Use lookahead to handle brackets in claim text properly
    checkmark_pattern = r"\(([✓✗])\)\s*(.+?)(?=\s*\[\d+%\s+confidence\]|$)"
    for match in re.finditer(checkmark_pattern, output, re.MULTILINE):
        status = match.group(1)
        text = match.group(2).strip()
        # Extract confidence if present
        conf_match = re.search(r"\[(\d+)%\s+confidence\]", output[match.end():match.end()+100])
        conf = int(conf_match.group(1)) / 100 if conf_match else (0.7 if status == "✓" else 0.3)
        
        claim_data = _build_claim_data(
            statement=text,
            confidence_model=conf,
            claim_type="verified" if status == "✓" else "refuted",
            claim_id=claim_counter,
        )
        claims.append(claim_data)
        claim_counter += 1
    
    return claims


def _build_claim_data(
    statement: str,
    confidence_model: float,
    claim_type: str,
    claim_id: int,
) -> dict:
    """Build complete claim data dict from extracted components.
    
    Generates all required Claim dataclass fields:
    - id: unique identifier
    - statement: the claim text
    - claim_type: inferred from context or provided
    - subject: extracted from statement
    - expected_value: dict with metadata
    - confidence_model: confidence score from output or defaults
    """
    # Generate unique claim ID
    claim_id_str = f"claim_{uuid.uuid4().hex[:8]}"
    
    # Extract subject (first capitalized noun or first significant word)
    subject = _extract_subject_from_statement(statement)
    
    # Build expected_value as metadata
    expected_value = {
        "inferred": True,
        "type": claim_type,
    }
    
    return {
        "id": claim_id_str,
        "statement": statement,
        "claim_type": claim_type,
        "subject": subject,
        "expected_value": expected_value,
        "confidence_model": max(0.0, min(1.0, confidence_model)),
    }


def _extract_subject_from_statement(statement: str) -> str:
    """Extract a subject identifier from the statement.
    
    Looks for:
    1. Known keywords (case-insensitive, ignoring apostrophes and trailing punctuation)
    2. Capitalized words (likely entities)
    3. First significant word
    """
    words = statement.split()
    
    # First pass: look for known keywords (case-insensitive)
    known_keywords = {'GPS', 'API', 'CPU', 'RAM', 'USB', 'HTTP', 'SQL', 'REST', 'JSON'}
    for word in words:
        # Remove possessive suffix ('s) and trailing punctuation for comparison
        clean_word = re.sub(r"'s$", "", word).rstrip(".,;:!?\"'").upper()
        if clean_word in known_keywords:
            return clean_word
    
    # Second pass: look for capitalized words (but skip common words)
    common_words = {'The', 'A', 'An', 'And', 'Or', 'But', 'This', 'That', 'Is'}
    for word in words:
        clean_word = word.rstrip(".,;:!?'\"")
        if clean_word not in common_words and clean_word and clean_word[0].isupper() and len(clean_word) > 2:
            return clean_word
    
    # Fallback: first significant word (at least 3 chars)
    for word in words:
        clean_word = word.rstrip(".,;:!?'\"")
        if len(clean_word) > 2:
            return clean_word
    
    return "unknown"


def _serialize_validation_data(validation: Optional[ValidationData]) -> Optional[dict]:
    """Convert ValidationData into a JSON-safe summary."""
    if validation is None:
        return None

    return {
        "claim_count": len(validation.claims),
        "validation_result_count": len(validation.validation_results),
        "hallucination_count": validation.hallucination_count,
        "overall_confidence": validation.overall_confidence,
        "contradictions": validation.contradictions,
        "hallucination_details": validation.hallucination_details,
    }


def _build_pass_result(
    pass_num: int,
    framing: str,
    tier: str,
    provider: str,
    model: str,
    output: str = "",
    validation: Optional[ValidationData] = None,
    error: Optional[str] = None,
) -> dict:
    """Build a serialized pass result with explicit success/failure state."""
    return asdict(
        PassResult(
            pass_num=pass_num,
            framing=framing,
            tier=tier,
            provider=provider,
            model=model,
            output=output,
            validation=_serialize_validation_data(validation),
            measured_confidence=validation.overall_confidence if validation else None,
            status="failed" if error else "complete",
            error=error,
        )
    )


def _successful_outputs(pass_results: list[dict]) -> list[str]:
    """Return only semantic outputs from successful passes."""
    return [
        pr["output"]
        for pr in pass_results
        if pr.get("status") == "complete"
        and isinstance(pr.get("output"), str)
        and pr.get("output").strip()
    ]


def _result_status(pass_results: list[dict]) -> str:
    """Summarize pass completion status for the overall result."""
    successes = sum(
        1
        for pr in pass_results
        if pr.get("status") == "complete"
        and isinstance(pr.get("output"), str)
        and pr.get("output").strip()
    )
    if successes == len(pass_results) and pass_results:
        return "complete"
    if successes:
        return "partial"
    return "failed"


# ---------------------------------------------------------------------------
# Ground truth validation (from engine.py lines 1432-1523)
# ---------------------------------------------------------------------------

async def _validate_claims_against_ground_truth(
    claims: list[dict],
    ground_truth_provider: Optional[Any] = None,
    timeout_secs: float = 10.0,
) -> Optional[ValidationData]:
    """Validate extracted claims against ground truth.
    
    Converts claim dicts to Claim objects and validates them.
    
    Returns ValidationData if ground_truth_provider is available, else None.
    
    Args:
        claims: List of claim dicts with 'id', 'statement', 'claim_type', 'subject', 
                'expected_value', 'confidence_model' fields
        ground_truth_provider: Provider instance with async validate_batch method
        timeout_secs: Timeout for validation batch operation (default 10.0 seconds)
    
    Raises:
        asyncio.TimeoutError: If validation takes longer than timeout_secs
        ValueError: If claim objects cannot be created
    """
    # TEMPORARY: Disable Nova verification due to authentication failures
    # Re-enable once Nova service is fixed
    if os.getenv("SKIP_VALIDATION"):
        log.info("Validation skipped (SKIP_VALIDATION=1)")
        return None
    
    if GroundTruthProvider is None or Claim is None or not ground_truth_provider:
        return None
    
    if not claims:
        return None
    
    try:
        # Convert claim dicts to Claim objects using dataclass constructor
        claim_objects = []
        for claim_data in claims:
            try:
                claim_obj = Claim(
                    id=claim_data.get("id", f"claim_{uuid.uuid4().hex[:8]}"),
                    statement=claim_data.get("statement", ""),
                    claim_type=claim_data.get("claim_type", "inferred"),
                    subject=claim_data.get("subject", "unknown"),
                    expected_value=claim_data.get("expected_value", {}),
                    confidence_model=float(claim_data.get("confidence_model", 0.5)),
                )
                claim_objects.append(claim_obj)
            except (TypeError, ValueError, KeyError) as e:
                log.warning(f"Failed to create Claim from data {claim_data}: {e}")
                continue
        
        if not claim_objects:
            log.debug("No valid claim objects after conversion")
            return None
        
        log.debug(f"Validating {len(claim_objects)} claims")
        
        # Validate batch with timeout to prevent hanging tasks
        try:
            validation_results = await asyncio.wait_for(
                ground_truth_provider.validate_batch(claim_objects),
                timeout=timeout_secs,
            )
        except asyncio.TimeoutError:
            log.warning(f"Validation batch timed out after {timeout_secs}s for {len(claim_objects)} claims")
            raise
        
        log.debug(f"Validation returned {len(validation_results) if validation_results else 0} results")
        
        if not validation_results:
            log.debug("Validation results are empty")
            return None
        
        # Aggregate results
        total = len(validation_results)
        hallucinations = sum(1 for r in validation_results if r.get("is_hallucination"))
        contradictions = [r for r in validation_results if r.get("is_contradiction")]
        avg_confidence = sum(r.get("grounding_confidence", 0.5) for r in validation_results) / max(total, 1)
        
        log.debug(f"Aggregated: {total} claims, {hallucinations} hallucinations, confidence {avg_confidence}")
        
        return ValidationData(
            claims=claim_objects,
            validation_results=validation_results,
            hallucination_count=hallucinations,
            overall_confidence=avg_confidence,
            contradictions=contradictions,
            hallucination_details=[],
        )
    
    except asyncio.TimeoutError:
        log.warning("Ground truth validation timed out")
        return None
    except ValueError as e:
        log.error(f"Failed to create claim objects: {e}")
        return None
    except Exception as e:
        log.debug(f"Ground truth validation failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Adaptive framing (from engine.py lines 1531-1638)
# ---------------------------------------------------------------------------

def _select_adaptive_framing(
    pass_number: int,
    total_passes: int,
    directives: list[tuple[str, str]],
    validation_result: Optional[ValidationData],
) -> tuple[str, str]:
    """Delegate to directives module."""
    return directives_module._select_adaptive_framing(
        pass_number=pass_number,
        total_passes=total_passes,
        directives=directives,
        validation_result=validation_result.to_dict() if validation_result else None,
    )


# ---------------------------------------------------------------------------
# Utility functions for fan-out (from engine.py lines 1905-1986)
# ---------------------------------------------------------------------------

def _extract_json_block(text: str, key: str = "answer") -> Optional[dict]:
    """Extract JSON block from text, optionally nested under a key."""
    # Try to find JSON object pattern
    json_pattern = r"\{[\s\S]*\}"
    match = re.search(json_pattern, text)
    
    if match:
        try:
            obj = json.loads(match.group(0))
            if key and key in obj:
                return obj[key]
            return obj
        except json.JSONDecodeError:
            return None
    
    return None


def _extract_claims(text: str) -> list[str]:
    """Extract key claims or conclusions from text."""
    claims = []
    
    # Pattern: "CLAIM: ..."
    for match in re.finditer(r"(?i)claim:\s*([^.\n]+)", text):
        claims.append(match.group(1).strip())
    
    # Pattern: "CONCLUSION: ..."
    for match in re.finditer(r"(?i)conclusion:\s*([^.\n]+)", text):
        claims.append(match.group(1).strip())
    
    # Pattern: "(key finding): ..."
    for match in re.finditer(r"\(key finding\):\s*([^.\n]+)", text):
        claims.append(match.group(1).strip())
    
    if not claims:
        # Fallback: take first 2 sentences
        sentences = re.split(r"[.!?]+", text)
        claims = [s.strip() for s in sentences[:2] if s.strip()]
    
    return claims[:5]  # Limit to 5 claims


async def _run_alarm_scan(
    pass_output: str,
    ground_truth_provider: Optional[Any] = None,
) -> Optional[dict]:
    """Run alarm scan to detect high-confidence hallucinations.
    
    Returns scan result dict with total_claims, hallucination_count, overall_confidence, etc.
    """
    claims = _extract_claims_from_pass_output(pass_output)
    validation = await _validate_claims_against_ground_truth(claims, ground_truth_provider)
    
    if validation:
        return {
            "total_claims": len(validation.claims),
            "hallucination_count": validation.hallucination_count,
            "overall_confidence": validation.overall_confidence,
            "contradictions": validation.contradictions,
        }
    
    return None


# ---------------------------------------------------------------------------
# Fan-out prompts (from engine.py lines 1000-1090)
# ---------------------------------------------------------------------------

_FAN_OUT_ALARM_PROMPT = """\
You are a contradiction detector. Below are {n} independent analyses of the same question.
Your ONLY task: identify explicit factual contradictions — cases where two perspectives make
directly incompatible claims about the same specific fact.

IGNORE: differences in emphasis, framing, confidence level, or opinion.
ONLY flag: direct factual contradictions (A says X is true, B says X is false).

Question analyzed: {question}

{perspectives}

Return ONLY valid JSON — no other text:
{{
  "contradictions": [
    {{
      "claim": "<the specific fact in dispute>",
      "perspective_a": "<name>",
      "says_a": "<what A claims>",
      "perspective_b": "<name>",
      "says_b": "<what B claims>"
    }}
  ]
}}

If no factual contradictions exist, return: {{"contradictions": []}}"""

_CLAIM_EXTRACTION_PROMPT = """\
Extract the key claims from this analysis. Be precise and concise.

Analysis to extract from:
{analysis}

Return ONLY valid JSON — no other text:
{{
  "claims": [
    {{
      "claim": "<specific factual or analytical claim, one sentence>",
      "confidence": <0.0-1.0, how confident the analysis seems in this claim>,
      "evidence_basis": "<brief note on what supports this claim, or 'asserted' if no support given>"
    }}
  ],
  "verdict": "<the analysis's overall conclusion in one sentence>",
  "key_uncertainties": ["<thing the analysis flagged as uncertain or unknown>"]
}}

Extract 3-7 claims. Focus on claims that are specific, falsifiable, and central to the verdict.
Do not include meta-commentary about the analysis process itself."""

_FAN_OUT_SYNTHESIS_PROMPT = """You are the synthesis analyst integrating {n} independent perspective analyses of the following question.

ORIGINAL QUESTION:
{question}

PERSPECTIVE ANALYSES:
{perspectives}

---
Analyze convergence and divergence across these perspectives, then produce your output as a JSON block.

INSTRUCTIONS:
1. CONVERGED CLAIMS: Identify claims where different perspectives independently reached the same conclusion through different reasoning paths. These are the highest-confidence findings.
2. CONTESTED AREAS: Identify claims where perspectives explicitly contradict each other — not just different emphasis, but actually conflicting factual assertions or conclusions.
3. CONFIDENCE SCORE: Rate overall confidence 0-100 based on: how many perspectives converged (more=higher), how many contested areas exist (more=lower), evidence quality, and internal consistency.
   - 80-100: Strong convergence, few or no contested areas
   - 60-79: Moderate convergence, some contested areas
   - 40-59: Mixed — significant divergence or uncertainty
   - 0-39: High divergence, contradictory evidence, or insufficient basis for conclusions
4. FINAL ANSWER: Integrate all perspectives into a concrete answer. Lead with converged high-confidence findings. Clearly mark contested claims. Note gaps.

Respond with ONLY this JSON (no other text before or after):
```json
{{
  "confidence_score": <integer 0-100>,
  "converged_claims": [
    "<specific claim that multiple perspectives independently agreed on>",
    "..."
  ],
  "contested_areas": [
    "<description of explicit conflict between perspectives, naming which perspectives disagree>",
    "..."
  ],
  "gaps": [
    "<important angle not addressed or insufficient evidence>",
    "..."
  ],
  "final_answer": "<full integrated synthesis — lead with convergence, mark contested areas, note remaining unknowns, give concrete conclusion>"
}}
```"""


# ---------------------------------------------------------------------------
# Deep think passes (from engine.py lines 1641-1897)
# ---------------------------------------------------------------------------

async def deep_think_passes(
    question: str,
    passes: int = 3,
    task_class: Optional[str] = None,
    data_policy: str = "any",
    model: Optional[str] = None,
    provider_config: Optional[dict] = None,
    ground_truth_provider: Optional[Any] = None,
    force_local_models: bool = False,
    device_id: str = "",
    job_id: str = "",
    pass_overrides: Optional[list] = None,
    mandate_prefix: str = "",
    verify: bool = False,
    perspective_name: str = "",
    enable_research: bool = True,
    research_query: Optional[str] = None,
    dama_node_id: str = "",
    dama_metric: str = "",
    web_domain_whitelist: Optional[list] = None,
) -> dict:
    """Main multi-pass reasoning loop.
    
    Args:
        question: The question or problem to reason about
        passes: Number of passes (default 3)
        task_class: Optional task class routing (auto-classified if None)
        data_policy: "any" | "local" | "cloud"
        model: Override model for all tiers
        provider_config: Per-call provider overrides
        ground_truth_provider: Optional ground truth validator
        force_local_models: When True, enforce local-only Ollama, block cloud providers.
                            Used for MQTT operations to prevent data leakage.
        device_id: Device ID for logging (e.g., "ant_001"). Used to tag MQTT enforcement logs.
        job_id: Optional job ID for tracking (from async job queue)
        pass_overrides: Per-pass overrides
        mandate_prefix: Mandate prefix for fan-out perspectives
        verify: Enable verification pass
        perspective_name: Perspective name for fan-out jobs
        enable_research: Accepted for API compatibility. This function runs pure LLM passes and
            has no internal tool loop. For fan-out jobs, enable_research=False suppresses
            enable_tool_use at the worker level before run_fan_out is called.
        research_query: Optional custom research query
        dama_node_id: DAMA device node ID for telemetry
        dama_metric: DAMA metric name
        web_domain_whitelist: Whitelist domains for web search
    
    Returns:
        Dict with keys: final_answer, pass_outputs, pass_results, confidence, duration_secs
    """
    import time
    import os
    
    start_time = time.time()
    
    # Validate parameters (Tier 1: prevent FastMCP slice object bugs)
    try:
        passes = validate_passes(passes)
    except ValidationError as e:
        error_msg = str(e) or type(e).__qualname__
        log.error(f"Parameter validation failed: {error_msg}")
        return {
            "error": str(e),
            "status": "validation_error",
            "final_answer": None,
            "pass_outputs": [],
            "pass_results": [],
            "confidence": 0,
            "duration_secs": 0,
        }
    
    # Check environment override for force_local_models (security gate)
    import os
    env_force_local = os.getenv("DEEP_THINK_FORCE_LOCAL", "1") != "0"
    force_local_models = force_local_models or env_force_local
    
    # Check for production security lock (strictest mode)
    ollama_only_mode = os.getenv("OLLAMA_ONLY_MODE", "0") != "0"
    if ollama_only_mode:
        force_local_models = True
        log.info("[SECURITY] OLLAMA_ONLY_MODE=1 detected, forcing local-only models")
    
    # Auto-classify if not provided
    if not task_class:
        policy_for_classification = str(data_policy).strip().lower() if data_policy else "any"
        task_class = await provider_module.classify_task(
            question,
            provider=provider_config.get("provider", "") if provider_config else "",
            data_policy=policy_for_classification,
        )
    
    # Get task profile
    if task_class not in directives_module.TASK_CLASS_PROFILES:
        log.warning(f"Unknown task class '{task_class}'; using 'general'")
        task_class = "general"
    
    task_profile = directives_module.TASK_CLASS_PROFILES[task_class]
    base_task_class = task_profile.get("task_class", task_class)
    directives = task_profile.get("directives", [])
    
    # Initialize provider config
    if provider_config is None:
        provider_config = {}
    else:
        provider_config = dict(provider_config)

    if task_profile.get("force_local"):
        force_local_models = True
        provider_config.setdefault("provider", "ollama")
        provider_config["light_provider"] = "ollama"
        provider_config["medium_provider"] = "ollama"
        provider_config["heavy_provider"] = "ollama"
        provider_config.setdefault("data_policy", "local")

    if task_profile.get("block_research_tools"):
        enable_research = False
    
    # Create provider config object
    cfg = provider_module.build_provider_config(provider_config)
    
    # Enforce local-only models if requested (async validation + setup)
    if force_local_models:
        await provider_module._validate_and_enforce_local_models(cfg, force_local_models, device_id)
        if device_id:
            log.info(f"[MQTT] Running local-only deep_think for device {device_id}")
    
    # Apply data_policy override
    if data_policy and data_policy != "any":
        cfg.data_policy = data_policy
    elif force_local_models:
        cfg.data_policy = "local"  # Ensure local is set

    for tier_name in ("light", "medium", "heavy"):
        tier_provider = provider_module._tier_provider(cfg, tier_name)
        tier_model = model or provider_module._model_for_tier(cfg, tier_name, task_class)
        enforce_task_class(base_task_class, tier_provider, [tier_model], job_id)
    
    # Run safety precheck if required
    if task_profile.get("safety_precheck"):
        safe, reason = await provider_module._run_safety_precheck(question, provider=cfg.provider)
        if not safe:
            log.warning(f"Safety precheck failed: {reason}")
            return {
                "final_answer": f"Request blocked by safety check: {reason}",
                "pass_outputs": [],
                "pass_results": [],
                "confidence": 0.0,
                "duration_secs": time.time() - start_time,
                "skill": task_class,
                "task_class": base_task_class,
            }
    
    pass_results = []
    validation_results = []

    # Compute a run signature that locks in all execution inputs for pass cache keying.
    _run_sig = hashlib.sha256(
        "\n".join([
            str(question),
            str(passes),
            str(task_class),
            repr(directives),
            str(mandate_prefix),
            str(cfg.data_policy),
        ]).encode()
    ).hexdigest()

    # Execute passes
    for pass_num in range(1, passes + 1):
        log.info(f"Pass {pass_num}/{passes}")
        
        # Check if this pass has overrides
        pass_override = None
        if pass_overrides and pass_num <= len(pass_overrides):
            pass_override = pass_overrides[pass_num - 1]  # 0-indexed
        
        # Select framing
        if pass_num == passes and len(directives) > 0:
            # Last pass: use final directive
            framing_name, framing_text = directives[-1]
        else:
            validation_data = validation_results[-1] if validation_results else None
            framing_name, framing_text = _select_adaptive_framing(
                pass_num, passes, directives, validation_data
            )
        
        # Apply override system prompt if provided
        if pass_override and "system" in pass_override:
            system_prompt = pass_override["system"]
        else:
            system_prompt = f"""You are an expert reasoner. Apply this framing strictly:

{framing_text}

Use the mandate to structure your response. Be precise and evidence-based."""
        
        user_prompt = f"Question: {question}"
        
        # Select provider and model with overrides
        override_tier = pass_override.get("tier") if pass_override else None
        tier = override_tier or directives_module._FRAMING_TIER.get(framing_name, "medium")
        
        override_provider = pass_override.get("provider") if pass_override else None
        provider_name = override_provider or provider_module._tier_provider(cfg, tier)
        
        override_model = pass_override.get("model") if pass_override else None
        model_name = override_model or (model or provider_module._model_for_tier(cfg, tier, task_class))
        log.info(f"deep_think_passes: pass {pass_num} model_name='{model_name}' for tier={tier}, task_class={task_class}, provider={provider_name}")
        
        # Fallback: ensure we have a valid Anthropic model
        if provider_name == "anthropic" and (not model_name or not model_name.startswith("claude")):
            log.warning(f"deep_think_passes: Invalid Anthropic model '{model_name}', falling back to claude-sonnet-4-6")
            model_name = "claude-sonnet-4-6"
        
        try:
            # Call provider
            output = await provider_module._call_provider(
                provider=provider_name,
                model=model_name,
                system=system_prompt,
                user_prompt=user_prompt,
                tier=tier,
                provider_config=provider_config,
            )

            # Extract and validate claims
            claims = _extract_claims_from_pass_output(output)
            validation = await _validate_claims_against_ground_truth(claims, ground_truth_provider)
            validation_results.append(validation)
            pass_results.append(
                _build_pass_result(
                    pass_num=pass_num,
                    framing=framing_name,
                    tier=tier,
                    provider=provider_name,
                    model=model_name,
                    output=output,
                    validation=validation,
                )
            )
            
            log.info(f"Pass {pass_num} complete ({framing_name})")

            # Persist pass output immediately for partial result recovery on crash.
            if job_id:
                await asyncio.to_thread(
                    store.set_pass_cache,
                    job_id, perspective_name, pass_num, _run_sig,
                    framing_name, tier, model_name, provider_name, output,
                )
        
        except Exception as e:
            # BUG FIX #1: Removed debug file write to /tmp (not available in k8s containers)
            # Ensure error_msg is never empty (some exceptions have empty str() representation)
            error_msg = str(e) or type(e).__qualname__ or f"Exception: {repr(e)}"
            _log_pass_exception(
                pass_num=pass_num,
                framing=framing_name,
                tier=tier,
                provider=provider_name,
                model=model_name,
                error=error_msg,
                exception_type=type(e).__qualname__,
                job_id=job_id,
                task_class=task_class,
                provider_config=provider_config,
                perspective=perspective_name if perspective_name else None,
            )
            log.error(f"Pass {pass_num} failed: {error_msg}", exc_info=True)
            validation_results.append(None)
            pass_results.append(
                _build_pass_result(
                    pass_num=pass_num,
                    framing=framing_name,
                    tier=tier,
                    provider=provider_name,
                    model=model_name,
                    error=error_msg,
                )
            )
    
    # Synthesize final answer
    pass_outputs = _successful_outputs(pass_results)
    final_answer = pass_outputs[-1] if pass_outputs else ""
    if base_task_class == "adversarial" and final_answer:
        final_answer = filter_adversarial_output(final_answer, job_id=job_id)
    
    # Calculate confidence
    confidences = [v.overall_confidence for v in validation_results if v]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.5
    
    duration = time.time() - start_time
    
    return {
        "status": _result_status(pass_results),
        "skill": task_class,
        "task_class": base_task_class,
        "final_answer": final_answer,
        "pass_outputs": pass_outputs,
        "pass_results": pass_results,
        "confidence": avg_confidence,
        "duration_secs": duration,
    }


# ---------------------------------------------------------------------------
# Fan-out helpers
# ---------------------------------------------------------------------------

_FILE_CITATION_RE = re.compile(r"\b[\w./\-]+\.\w{1,10}:\d+\b")


def _validate_synthesis_grounding(
    synthesis_text: str,
    tools_invoked_total: int,
    successful_tool_calls: int,
    enable_tool_use: bool,
    task_class: str,
) -> tuple[bool, list[str]]:
    """Return (inference_only, grounding_warnings).

    inference_only=True means no real file/tool evidence backed the synthesis.
    grounding_warnings contains human-readable messages for each failure.
    """
    warnings: list[str] = []
    inference_only = False

    if enable_tool_use and successful_tool_calls == 0:
        inference_only = True
        warnings.append(
            "GROUNDING UNAVAILABLE: enable_tool_use=True but no tool calls returned evidence. "
            "All findings are model inference only — not backed by file evidence."
        )

    if task_class == "code_review" and successful_tool_calls == 0:
        if not _FILE_CITATION_RE.search(synthesis_text):
            inference_only = True
            if not any("GROUNDING UNAVAILABLE" in w for w in warnings):
                warnings.append(
                    "CITATION WARNING: code_review synthesis contains no file-path:line "
                    "citations. Findings may be fabricated. Do not treat as verified."
                )

    return inference_only, warnings


def _fan_out_parse_json(text: str) -> dict | None:
    """Extract a JSON object from model output, stripping markdown fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    if "```json" in text:
        inner = text.split("```json", 1)[1].split("```", 1)[0].strip()
        try:
            return json.loads(inner)
        except Exception:
            pass
    if "```" in text:
        inner = text.split("```", 1)[1].split("```", 1)[0].strip()
        try:
            return json.loads(inner)
        except Exception:
            pass
    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except Exception:
                        break
    return None


async def _fan_out_alarm_scan(question, successes, cfg, task_class, provider_config):
    """LLM-based contradiction scan across perspective outputs."""
    if len(successes) < 2:
        return []
    perspectives_text = "\n\n".join(
        f"=== {p['name'].upper()} ===\n{p['final_answer']}"
        for p in successes
    )
    prompt = _FAN_OUT_ALARM_PROMPT.format(n=len(successes), question=question, perspectives=perspectives_text)
    try:
        provider = provider_module._tier_provider(cfg, "medium")
        model = provider_module._model_for_tier(cfg, "medium", task_class)
        raw = await provider_module._call_provider(
            provider=provider, model=model,
            system="You are a factual contradiction detector. Return only valid JSON.",
            user_prompt=prompt,
            tier="medium", provider_config=provider_config,
        )
        parsed = _fan_out_parse_json(raw)
        if parsed and isinstance(parsed.get("contradictions"), list):
            return parsed["contradictions"]
        return []
    except Exception as exc:
        log.warning("Fan-out alarm scan failed (non-fatal): %s", exc)
        return []


async def _fan_out_extract_claims(perspective_name, analysis_text, cfg, task_class, provider_config):
    """Distil perspective prose into structured claim set for synthesis compression."""
    if not analysis_text or not analysis_text.strip():
        return {"claims": [], "verdict": "", "key_uncertainties": []}
    prompt = _CLAIM_EXTRACTION_PROMPT.format(analysis=analysis_text[:4000])
    try:
        provider = provider_module._tier_provider(cfg, "light")
        model = provider_module._model_for_tier(cfg, "light", "extraction")
        raw = await provider_module._call_provider(
            provider=provider, model=model,
            system="Extract claims in JSON format. Return only valid JSON.",
            user_prompt=prompt,
            tier="light", provider_config=provider_config,
        )
        parsed = _fan_out_parse_json(raw)
        if parsed and isinstance(parsed.get("claims"), list):
            return parsed
        return {"claims": [], "verdict": raw[:200], "key_uncertainties": []}
    except Exception as exc:
        log.warning("Claim extraction failed for %s (non-fatal): %s", perspective_name, exc)
        return {"claims": [], "verdict": "", "key_uncertainties": []}


# ---------------------------------------------------------------------------
# Fan-out reasoning (from engine.py lines 2036-2488)
# ---------------------------------------------------------------------------

async def run_fan_out(
    question: str,
    width: int = 3,
    height: int = 2,
    provider_cfg: Optional[ProviderConfig] = None,
    provider_cfgs: Optional[list] = None,
    provider_config: Optional[dict] = None,
    task_class: Optional[str] = None,
    data_policy: str = "any",
    model: Optional[str] = None,
    max_parallel: int = 2,
    job_id: str = "",
    max_width: int = 6,
    confidence_threshold: int = 50,
    extract_claims: bool = False,
    topology: str = "static",
    adaptive_config: Optional[dict] = None,
    enable_tool_use: bool = False,
    tool_evidence_weight: float = DEFAULT_TOOL_EVIDENCE_WEIGHT,
    force_local_models: bool = False,
    device_id: str = "",
    web_domain_whitelist: Optional[list[str]] = None,
    ground_truth_provider: Optional[Any] = None,
) -> dict:
    """Fan-out reasoning with parallel perspectives and synthesis.

    Runs `width` parallel mandate-driven agents x `height` passes each.
    Final heavy synthesis pass integrates all perspectives.

    Adaptive expansion: if confidence_score < confidence_threshold OR contested_areas > 2,
    dispatches remaining unused mandates and re-synthesizes (max 1 expansion).

    Args:
        question:             The question or content to analyze.
        width:                Parallel perspectives (1-6).
        height:               Sequential passes per perspective (1-5).
        provider_cfg:         ProviderConfig object (preferred).
        provider_cfgs:        List of ProviderConfig for round-robin.
        provider_config:      Dict provider overrides (legacy fallback when provider_cfg is None).
        task_class:           Mandate set to use.
        data_policy:          "any" | "local" | "cloud"
        max_parallel:         Max concurrent perspectives (default 2).
        job_id:               Job ID for pass caching.
        max_width:            Expansion ceiling (default 6).
        confidence_threshold: Adaptive expansion trigger (default 50).
        extract_claims:       Compress perspectives to claim sets before synthesis.
        topology:             "static" (default) or "adaptive" (enables tool use).
        adaptive_config:      Tool loop configuration dict.
        enable_tool_use:      If True and topology=="adaptive" or task_class=="code_review", run tools.
        tool_evidence_weight: Evidence weight for tool results (0.0-1.0).
        force_local_models:   MQTT safety: force Ollama-only.
        device_id:            MQTT device ID for logging.
        ground_truth_provider: Optional ground truth validator.

    Returns:
        dict with type="fan_out", perspectives, synthesis fields, quality metrics.
    """
    import time
    start_time = time.time()

    try:
        width = validate_width(width)
        height = validate_height(height)
    except ValidationError as e:
        return {
            "error": str(e), "status": "validation_error",
            "final_answer": None, "perspective_outputs": {},
            "synthesis": None, "confidence": 0, "duration_secs": 0,
        }

    env_force_local = os.getenv("DEEP_THINK_FORCE_LOCAL", "1") != "0"
    force_local_models = force_local_models or env_force_local
    if os.getenv("OLLAMA_ONLY_MODE", "0") != "0":
        force_local_models = True

    _pc = dict(provider_config or {})
    if force_local_models:
        _pc.setdefault("provider", "ollama")
        _pc["light_provider"] = "ollama"
        _pc["medium_provider"] = "ollama"
        _pc["heavy_provider"] = "ollama"
        _pc["data_policy"] = "local"
        data_policy = "local"
    elif data_policy and data_policy != "any":
        _pc["data_policy"] = data_policy

    # Build provider pool
    _cfg_pool: list
    if provider_cfgs and len(provider_cfgs) > 0:
        _cfg_pool = provider_cfgs
    elif provider_cfg is not None:
        _cfg_pool = [provider_cfg]
    else:
        _cfg_pool = [provider_module.build_provider_config(_pc)]

    if data_policy != "any":
        _cfg_pool = [dataclasses_replace(c, data_policy=data_policy) for c in _cfg_pool]

    cfg = _cfg_pool[0]

    width = max(1, min(width, 6))
    height = max(1, min(height, 5))

    if not task_class:
        task_class = await provider_module.classify_task(question, provider=_pc.get("provider", ""))

    if task_class not in directives_module.TASK_CLASS_PROFILES:
        task_class = "general"

    task_profile = directives_module.TASK_CLASS_PROFILES[task_class]
    base_task_class = task_profile.get("task_class", task_class)
    resolved_class = task_class

    if task_profile.get("force_local"):
        force_local_models = True
        _pc.setdefault("provider", "ollama")
        _pc["light_provider"] = "ollama"
        _pc["medium_provider"] = "ollama"
        _pc["heavy_provider"] = "ollama"
        _pc["data_policy"] = "local"

    if force_local_models:
        await provider_module._validate_and_enforce_local_models(cfg, force_local_models, device_id)
        if device_id:
            log.info("[MQTT] Running local-only fan-out for device %s", device_id)

    _mandates_raw = directives_module.PERSPECTIVE_MANDATES.get(resolved_class, directives_module.PERSPECTIVE_MANDATES["general"])
    if isinstance(_mandates_raw, dict):
        _mandates_raw = [{"name": k, "mandate": v} for k, v in _mandates_raw.items()]
    mandates = _mandates_raw[:width]

    tool_mode_enabled = bool(enable_tool_use) and (
        topology == "adaptive" or resolved_class == "code_review"
    )
    adaptive_cfg = adaptive_config or {}
    max_tools_global = int(adaptive_cfg.get("max_tool_calls_global", 20))
    max_tools_per_perspective = int(adaptive_cfg.get("max_tool_calls_per_perspective", 5))
    tool_budget = max(0, min(max_tools_global, max_tools_per_perspective))
    tool_timeout = int(adaptive_cfg.get("tool_timeout", 30))
    evidence_confidence = max(0.0, min(float(tool_evidence_weight), 1.0))

    pool_desc = (
        "+".join(c.provider for c in _cfg_pool) if len(_cfg_pool) > 1 else _cfg_pool[0].provider
    )
    log.info("Fan-out: width=%d height=%d task_class=%s providers=%s topology=%s",
             width, height, resolved_class, pool_desc, topology)

    sem = asyncio.Semaphore(max(1, min(max_parallel, width)))

    def _perspective_cache_key(mandate_text: str, perspective_cfg: ProviderConfig) -> str:
        sig = provider_module.model_summary(perspective_cfg, resolved_class)
        payload = f"{question}\n---\n{mandate_text}\n---h{height}\n---{sig}"
        return hashlib.sha256(payload.encode()).hexdigest()

    async def _run_tool_phase(perspective_name: str, perspective_answer: str, perspective_confidence: float = 0.5):
        """Run optional per-perspective tool loop. Returns (tools_invoked, tool_errors, evidence_summary)."""
        if not tool_mode_enabled or tool_budget <= 0:
            return [], [], ""
        try:
            from deep_think_mcp.executor import queue_tools, invoke_tools_and_digest
            from deep_think_mcp.models_adaptive import RoutingAction, RoutingDecision, ToolDirective
            from deep_think_mcp.models_executor import ExecutionConfig
        except ImportError as e:
            log.warning("Tool phase unavailable (import error): %s", e)
            return [], [f"import_error:{e}"], ""

        tool_name = "code_search" if resolved_class == "code_review" else "web_search"
        if not check_research_tool_allowed(resolved_class, tool_name, job_id=job_id):
            return [], [f"blocked_tool:{tool_name}"], ""
        tool_query = _build_tool_query(question, perspective_answer)
        if not tool_query:
            return [], ["missing tool query"], ""

        try:
            routing_decision = RoutingDecision(
                perspective_id=perspective_name,
                action=RoutingAction.CONTINUE_WITH_TOOLS.value,
                recommended_tools=[
                    ToolDirective(
                        tool_name=tool_name,
                        query=tool_query,
                        reason="fan_out_tool_loop",
                        priority=1,
                        max_results=5,
                        timeout=tool_timeout,
                    )
                ],
                decision_basis=["fan_out_tool_loop_integration"],
            )
            run_budget = max(tool_budget, 2)
            exec_cfg = ExecutionConfig(
                tool_timeout=tool_timeout,
                min_budget_to_invoke_tools=1,
            )
            queued_tools, estimated_budget_cost = await asyncio.to_thread(
                queue_tools, routing_decision, run_budget, exec_cfg,
            )
            if not queued_tools or estimated_budget_cost <= 0:
                return [], [], ""

            evidence_digest, _ = await asyncio.to_thread(
                invoke_tools_and_digest,
                queued_tools, perspective_name, run_budget,
                perspective_confidence, exec_cfg, estimated_budget_cost,
                task_class=resolved_class,
                job_id=job_id,
                web_domain_whitelist=web_domain_whitelist or [],
            )
            tools_invoked = [tool["tool_name"] for tool in queued_tools]
            if evidence_digest is None:
                return tools_invoked, ["tool invocation failed"], ""

            tool_errors = [
                f"{entry.tool_name}:{entry.tool_status}"
                for entry in evidence_digest.entries
                if entry.tool_status != "success"
            ]
            return tools_invoked, tool_errors, (evidence_digest.formatted_summary or "")
        except Exception as exc:
            log.warning("Tool phase failed for %s (non-fatal): %s", perspective_name, exc)
            return [], [f"tool_phase_error:{exc}"], ""

    async def run_perspective(mandate: dict, slot: int) -> dict:
        perspective_cfg = _cfg_pool[slot % len(_cfg_pool)]
        name = mandate["name"]
        mandate_text = f"[Perspective: {name.upper()}]\n{mandate['mandate']}"
        cache_key = _perspective_cache_key(mandate_text, perspective_cfg)

        cached = await asyncio.to_thread(store.get_perspective_cache, cache_key)
        if cached and cached.get("final_answer"):
            log.info("Fan-out perspective %s: cache HIT (key=%s...)", name, cache_key[:12])
            if job_id:
                await asyncio.to_thread(
                    store.set_pass_cache,
                    job_id, name, 1, cache_key,
                    "perspective_cache_hit", "cached", "cached", "cached",
                    cached["final_answer"],
                )
            return {
                "name": name, "status": "complete",
                "final_answer": cached["final_answer"],
                "passes_run": cached["passes_run"],
                "cache_hit": True,
                "tools_invoked": [], "tool_errors": [], "evidence_summary": "",
            }

        async with sem:
            log.debug("Fan-out perspective starting: %s (slot=%d provider=%s)",
                      name, slot, perspective_cfg.provider)

            perspective_pc = asdict(perspective_cfg)

            r = await deep_think_passes(
                question=question,
                passes=height,
                provider_config=perspective_pc,
                task_class=resolved_class,
                data_policy=data_policy,
                mandate_prefix=mandate_text,
                job_id=job_id,
                perspective_name=name,
                force_local_models=force_local_models,
                device_id=device_id,
            )

        child_status = "complete"
        child_error = None
        perspective_confidence = 0.5  # default mid-point sentinel
        if isinstance(r, dict):
            child_status = str(r.get("status") or "failed")
            child_error = r.get("error")
            final_answer = r.get("final_answer", "")
            passes_run = len(r.get("pass_results", []))
            raw_conf = r.get("confidence", 0.5)
            perspective_confidence = max(0.0, min(1.0, float(raw_conf) if raw_conf else 0.5))
        else:
            final_answer = str(r)
            passes_run = height

        if child_status == "complete" and isinstance(final_answer, str) and final_answer.strip():
            tools_invoked, tool_errors, evidence_summary = await _run_tool_phase(name, final_answer, perspective_confidence)
            perspective_model_sig = provider_module.model_summary(perspective_cfg, resolved_class)
            await asyncio.to_thread(
                store.set_perspective_cache,
                cache_key, name, final_answer, perspective_model_sig, passes_run, job_id,
            )
        else:
            tools_invoked, tool_errors, evidence_summary = [], [], ""
            if child_error is None and child_status != "complete":
                child_error = f"perspective returned status={child_status}"

        return {
            "name": name,
            "status": child_status,
            "error": child_error,
            "final_answer": final_answer,
            "passes_run": passes_run,
            "cache_hit": False,
            "tools_invoked": tools_invoked,
            "tool_errors": tool_errors,
            "evidence_summary": evidence_summary,
        }

    # Run all perspectives
    raw_results = await asyncio.gather(
        *[run_perspective(m, slot=i) for i, m in enumerate(mandates)],
        return_exceptions=True,
    )

    perspective_outputs: list = []
    for mandate, result in zip(mandates, raw_results):
        if isinstance(result, Exception):
            log.error("Fan-out perspective %s failed: %s", mandate["name"], result)
            perspective_outputs.append({
                "name": mandate["name"], "status": "failed",
                "error": str(result), "final_answer": None,
                "tools_invoked": [], "tool_errors": [], "evidence_summary": "",
            })
        else:
            perspective_outputs.append(result)

    def _format_perspective_output(p: dict) -> dict:
        synthesis_text = p.get("final_answer")
        if synthesis_text is None:
            synthesis_text = ""
        return {
            "synthesis": synthesis_text,
            "status": p.get("status", "failed"),
            "error": p.get("error"),
            "cache_hit": p.get("cache_hit", False),
            "tools_invoked": p.get("tools_invoked", []),
            "tool_errors": p.get("tool_errors", []),
            "evidence_summary": p.get("evidence_summary", ""),
        }

    successes = [p for p in perspective_outputs if p["status"] == "complete" and p["final_answer"]]
    if len(successes) < max(1, width // 2):
        duration = time.time() - start_time
        cache_hits = sum(1 for p in perspective_outputs if p.get("cache_hit"))
        tools_invoked_total = sum(len(p.get("tools_invoked", [])) for p in perspective_outputs)
        successful_tool_calls = sum(
            max(len(p.get("tools_invoked", [])) - len(p.get("tool_errors", [])), 0)
            for p in perspective_outputs
        )
        log.error(
            "Fan-out failed: only %d/%d perspectives succeeded.",
            len(successes), width,
        )
        return {
            "type": "fan_out",
            "status": "failed",
            "task_class": resolved_class,
            "skill": resolved_class,
            "width": width,
            "height": height,
            "perspectives_attempted": width,
            "perspectives_succeeded": len(successes),
            "cache_hits": cache_hits,
            "tools_invoked_total": tools_invoked_total,
            "tool_successes_total": successful_tool_calls,
            "inference_only": False,
            "grounding_warnings": [],
            "adaptive_triggered": False,
            "adaptive_reason": "",
            "final_width": width,
            "alarm_signals": [],
            "provider": pool_desc,
            "confidence_score": None,
            "converged_claims": [],
            "contested_areas": [],
            "gaps": [],
            "final_answer": "",
            "perspectives": [
                {
                    "name": p["name"], "status": p["status"],
                    "final_answer": p.get("final_answer"),
                    "cache_hit": p.get("cache_hit", False),
                    "tools_invoked": p.get("tools_invoked", []),
                    "tool_errors": p.get("tool_errors", []),
                    "evidence_summary": p.get("evidence_summary", ""),
                    "error": p.get("error"),
                }
                for p in perspective_outputs
            ],
            "claim_sets": [],
            "topology": topology,
            "adaptive_config": adaptive_config or {},
            "enable_tool_use": enable_tool_use,
            "tool_evidence_weight": tool_evidence_weight,
            "confidence": 0.0,
            "duration_secs": duration,
            "perspective_outputs": {p["name"]: _format_perspective_output(p) for p in perspective_outputs},
        }

    # LLM-based contradiction scan
    alarm_signals = await _fan_out_alarm_scan(question, successes, cfg, resolved_class, _pc)

    # Optional claim extraction before synthesis
    claim_sets: list = []
    if extract_claims and len(successes) >= 1:
        extract_tasks = [
            _fan_out_extract_claims(p["name"], p["final_answer"] or "", cfg, resolved_class, _pc)
            for p in successes
        ]
        claim_sets = list(await asyncio.gather(*extract_tasks, return_exceptions=False))
        claim_sets = [
            cs if isinstance(cs, dict) else {"claims": [], "verdict": "", "key_uncertainties": []}
            for cs in claim_sets
        ]
        log.info("Claim extraction: %d perspectives, total claims=%d",
                 len(claim_sets), sum(len(cs.get("claims", [])) for cs in claim_sets))

    # Build synthesis prompt
    if extract_claims and claim_sets:
        compact_parts = []
        for p, cs in zip(successes, claim_sets):
            evidence_block = f"\nTOOL EVIDENCE:\n{p['evidence_summary']}" if p.get("evidence_summary") else ""
            claims_fmt = "\n".join(
                f"  - [{(c.get('confidence') or 0):.0%}] {c.get('claim', '')} "
                f"(basis: {c.get('evidence_basis', 'asserted') or 'asserted'})"
                for c in cs.get("claims", [])
            )
            uncertainties_fmt = (
                "\n".join(f"  ? {u}" for u in cs.get("key_uncertainties", []))
                or "  (none flagged)"
            )
            compact_parts.append(
                f"=== {p['name'].upper()} PERSPECTIVE ===\n"
                f"VERDICT: {cs.get('verdict', '(none)')}\n"
                f"CLAIMS:\n{claims_fmt or '  (no claims extracted)'}\n"
                f"UNCERTAINTIES:\n{uncertainties_fmt}"
                f"{evidence_block}"
            )
        perspectives_text = "\n\n".join(compact_parts)
    else:
        perspectives_text = "\n\n".join(
            f"=== {p['name'].upper()} PERSPECTIVE ===\n{p['final_answer']}"
            + (f"\n\nTOOL EVIDENCE:\n{p['evidence_summary']}" if p.get("evidence_summary") else "")
            for p in successes
        )

    if alarm_signals:
        alarm_preamble = (
            "⚠️ CONTRADICTION ALERTS — the following factual conflicts were detected "
            "between perspectives. Address each explicitly in your synthesis:\n"
        )
        for i, sig in enumerate(alarm_signals, 1):
            alarm_preamble += (
                f"\n{i}. CLAIM: {sig.get('claim', '?')}\n"
                f"   {sig.get('perspective_a', '?')} says: {sig.get('says_a', '?')}\n"
                f"   {sig.get('perspective_b', '?')} says: {sig.get('says_b', '?')}\n"
            )
        perspectives_text = alarm_preamble + "\n\n" + perspectives_text

    synthesis_question = _FAN_OUT_SYNTHESIS_PROMPT.format(
        n=len(successes), question=question, perspectives=perspectives_text,
    )

    synthesis_cfg_pc = asdict(cfg)
    synthesis_status = "failed"
    synthesis_error: Optional[str] = None

    def _assess_synthesis_health(result_obj: Any, synthesis_text_value: str) -> tuple[str, Optional[str], bool]:
        status_value = "complete"
        error_value = None
        if isinstance(result_obj, dict):
            status_value = str(result_obj.get("status") or "failed")
            if result_obj.get("error") is not None:
                error_value = str(result_obj.get("error"))
        has_answer = isinstance(synthesis_text_value, str) and bool(synthesis_text_value.strip())
        if not has_answer and error_value is None:
            error_value = "synthesis returned empty final_answer"
        is_healthy = status_value == "complete" and has_answer
        return status_value, error_value, is_healthy

    async def _run_synthesis_with_fallback(
        synth_question: str,
        synth_cfg: dict,
        perspective_name: str,
    ) -> Any:
        """Run synthesis, falling back to medium-tier model on first failure."""
        try:
            return await deep_think_passes(
                question=synth_question, passes=1,
                provider_config=synth_cfg,
                task_class="synthesis",
                data_policy=data_policy,
                job_id=job_id,
                perspective_name=perspective_name,
            )
        except Exception as primary_exc:
            log.warning(
                "Fan-out synthesis (%s) failed with primary config: %s — retrying with medium tier",
                perspective_name, primary_exc,
            )
            fallback_cfg = dict(synth_cfg)
            fallback_cfg["heavy"] = fallback_cfg.get("medium", "")
            try:
                return await deep_think_passes(
                    question=synth_question, passes=1,
                    provider_config=fallback_cfg,
                    task_class="synthesis",
                    data_policy=data_policy,
                    job_id=job_id,
                    perspective_name=f"{perspective_name}_fallback",
                )
            except Exception as fallback_exc:
                log.error(
                    "Fan-out synthesis (%s) fallback also failed: %s",
                    perspective_name, fallback_exc,
                )
                return {"final_answer": "", "status": "failed", "error": str(fallback_exc)}

    synthesis_result = await _run_synthesis_with_fallback(
        synth_question=synthesis_question,
        synth_cfg=synthesis_cfg_pc,
        perspective_name="synthesis",
    )

    raw_answer = synthesis_result.get("final_answer", "") if isinstance(synthesis_result, dict) else str(synthesis_result)
    synthesis_structured = _fan_out_parse_json(raw_answer)
    if synthesis_structured:
        synthesis_text = synthesis_structured.get("final_answer", raw_answer)
        log.debug("Fan-out synthesis parsed: confidence=%s contested=%d converged=%d",
                  synthesis_structured.get("confidence_score"),
                  len(synthesis_structured.get("contested_areas", [])),
                  len(synthesis_structured.get("converged_claims", [])))
    else:
        log.warning("Fan-out: synthesis JSON parse failed — falling back to plain text")
        synthesis_text = raw_answer

    synthesis_status, synthesis_error, synthesis_healthy = _assess_synthesis_health(
        synthesis_result, synthesis_text
    )

    confidence_score = synthesis_structured.get("confidence_score") if synthesis_structured else None
    converged_claims = synthesis_structured.get("converged_claims", []) if synthesis_structured else []
    contested_areas = synthesis_structured.get("contested_areas", []) if synthesis_structured else []
    gaps = synthesis_structured.get("gaps", []) if synthesis_structured else []

    # Adaptive expansion
    adaptive_triggered = False
    adaptive_reason = ""
    final_width = width
    all_successes = successes  # will grow if adaptive expansion runs

    _all_mandates_raw = directives_module.PERSPECTIVE_MANDATES.get(resolved_class, directives_module.PERSPECTIVE_MANDATES["general"])
    if isinstance(_all_mandates_raw, dict):
        _all_mandates_raw = [{"name": k, "mandate": v} for k, v in _all_mandates_raw.items()]
    all_mandates = _all_mandates_raw
    unused_mandates = [m for m in all_mandates if m not in mandates]

    should_expand = (
        unused_mandates
        and width < max_width
        and (
            (confidence_score is not None and confidence_score < confidence_threshold)
            or len(contested_areas) > 2
        )
    )

    if should_expand:
        expansion_width = min(len(unused_mandates), max_width - width)
        expansion_mandates = unused_mandates[:expansion_width]
        log.info("Adaptive expansion: confidence=%s contested=%d — adding %d perspectives",
                 confidence_score, len(contested_areas), expansion_width)
        adaptive_triggered = True
        adaptive_reason = (
            f"confidence_score={confidence_score} < threshold={confidence_threshold}"
            if confidence_score is not None and confidence_score < confidence_threshold
            else f"contested_areas={len(contested_areas)} > 2"
        )

        expansion_start_slot = len(mandates)
        extra_results = await asyncio.gather(
            *[run_perspective(m, slot=expansion_start_slot + i)
              for i, m in enumerate(expansion_mandates)],
            return_exceptions=True,
        )
        extra_outputs = []
        for mandate, result in zip(expansion_mandates, extra_results):
            if isinstance(result, Exception):
                log.error("Adaptive perspective %s failed: %s", mandate["name"], result)
                extra_outputs.append({"name": mandate["name"], "status": "failed", "error": str(result), "final_answer": None})
            else:
                extra_outputs.append(result)

        extra_successes = [p for p in extra_outputs if p["status"] == "complete" and p["final_answer"]]
        all_successes = successes + extra_successes
        perspective_outputs = perspective_outputs + extra_outputs
        final_width = width + len(extra_outputs)

        if extra_successes:
            alarm_signals = await _fan_out_alarm_scan(question, all_successes, cfg, resolved_class, _pc)
            perspectives_text = "\n\n".join(
                f"=== {p['name'].upper()} PERSPECTIVE ===\n{p['final_answer']}"
                + (f"\n\nTOOL EVIDENCE:\n{p['evidence_summary']}" if p.get("evidence_summary") else "")
                for p in all_successes
            )
            if alarm_signals:
                alarm_preamble = "⚠️ CONTRADICTION ALERTS:\n"
                for i, sig in enumerate(alarm_signals, 1):
                    alarm_preamble += (
                        f"{i}. {sig.get('claim','?')}: "
                        f"{sig.get('perspective_a','?')} says '{sig.get('says_a','?')}' vs "
                        f"{sig.get('perspective_b','?')} says '{sig.get('says_b','?')}'\n"
                    )
                perspectives_text = alarm_preamble + "\n\n" + perspectives_text

            synthesis_question = _FAN_OUT_SYNTHESIS_PROMPT.format(
                n=len(all_successes), question=question, perspectives=perspectives_text,
            )
            synthesis_result = await _run_synthesis_with_fallback(
                synth_question=synthesis_question,
                synth_cfg=synthesis_cfg_pc,
                perspective_name="synthesis_adaptive",
            )
            raw_answer = synthesis_result.get("final_answer", "") if isinstance(synthesis_result, dict) else str(synthesis_result)
            synthesis_structured = _fan_out_parse_json(raw_answer)
            if synthesis_structured:
                synthesis_text = synthesis_structured.get("final_answer", raw_answer)
                confidence_score = synthesis_structured.get("confidence_score")
                converged_claims = synthesis_structured.get("converged_claims", [])
                contested_areas = synthesis_structured.get("contested_areas", [])
                gaps = synthesis_structured.get("gaps", [])
            else:
                synthesis_text = raw_answer
            synthesis_status, synthesis_error, synthesis_healthy = _assess_synthesis_health(
                synthesis_result, synthesis_text
            )

    cache_hits = sum(1 for p in perspective_outputs if p.get("cache_hit"))
    tools_invoked_total = sum(len(p.get("tools_invoked", [])) for p in perspective_outputs)
    successful_tool_calls = sum(
        max(len(p.get("tools_invoked", [])) - len(p.get("tool_errors", [])), 0)
        for p in perspective_outputs
    )

    # Grounding gate
    inference_only, grounding_warnings = _validate_synthesis_grounding(
        synthesis_text=synthesis_text,
        tools_invoked_total=tools_invoked_total,
        successful_tool_calls=successful_tool_calls,
        enable_tool_use=enable_tool_use,
        task_class=resolved_class,
    )
    if grounding_warnings:
        warning_block = "\n".join(f"⚠️  {w}" for w in grounding_warnings)
        synthesis_text = f"{warning_block}\n\n{synthesis_text}"
        log.warning("Fan-out grounding gate: inference_only=%s warnings=%s", inference_only, grounding_warnings)

    duration = time.time() - start_time

    overall_status = (
        "complete"
        if successes and synthesis_healthy
        else "failed"
    )

    return {
        "type": "fan_out",
        "status": overall_status,
        "synthesis_status": synthesis_status,
        "synthesis_error": synthesis_error,
        "task_class": resolved_class,
        "skill": resolved_class,
        "width": width,
        "height": height,
        "perspectives_attempted": final_width,
        "perspectives_succeeded": len(all_successes),
        "cache_hits": cache_hits,
        "tools_invoked_total": tools_invoked_total,
        "tool_successes_total": successful_tool_calls,
        "inference_only": inference_only,
        "grounding_warnings": grounding_warnings,
        "adaptive_triggered": adaptive_triggered,
        "adaptive_reason": adaptive_reason,
        "final_width": final_width,
        "alarm_signals": alarm_signals,
        "provider": pool_desc,
        "confidence_score": confidence_score,
        "converged_claims": converged_claims,
        "contested_areas": contested_areas,
        "gaps": gaps,
        "perspectives": [
            {
                "name": p["name"],
                "status": p["status"],
                "final_answer": p.get("final_answer"),
                "error": p.get("error"),
                "cache_hit": p.get("cache_hit", False),
                "tools_invoked": p.get("tools_invoked", []),
                "tool_errors": p.get("tool_errors", []),
                "evidence_summary": p.get("evidence_summary", ""),
            }
            for p in perspective_outputs
        ],
        "claim_sets": claim_sets if extract_claims else [],
        "topology": topology,
        "adaptive_config": adaptive_config or {},
        "enable_tool_use": enable_tool_use,
        "tool_evidence_weight": tool_evidence_weight,
        "final_answer": synthesis_text,
        "confidence": float(confidence_score) / 100.0 if confidence_score is not None else 0.7,
        "duration_secs": duration,
        "perspective_outputs": {p["name"]: _format_perspective_output(p) for p in perspective_outputs},
    }
