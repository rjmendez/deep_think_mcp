"""Main orchestration loop and fan-out reasoning implementation.

Handles:
- deep_think_passes: Main multi-pass reasoning loop
- run_fan_out: Parallel perspective reasoning with synthesis
- Utility functions for pass execution, claim extraction, validation
"""

import asyncio
import json
import logging
import re
import uuid
from typing import Optional, Any

try:
    from ground_truth import GroundTruthProvider, Claim
except ImportError:
    GroundTruthProvider = None
    Claim = None

from .types import ProviderConfig, PassResult, ValidationData
from . import provider as provider_module
from . import directives as directives_module
from deep_think_mcp import store
from deep_think_mcp import discover

log = logging.getLogger(__name__)


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
    claim_pattern = r"(?i)claim:\s*([^[\n]+)(?:\[confidence:\s*(\d+)%\])?"
    for match in re.finditer(claim_pattern, output):
        text = match.group(1).strip()
        conf = int(match.group(2)) / 100 if match.group(2) else 0.5
        
        claim_data = _build_claim_data(
            statement=text,
            confidence_model=conf,
            claim_type="inferred",
            claim_id=claim_counter,
        )
        claims.append(claim_data)
        claim_counter += 1
    
    # Pattern 2: "(✓) ... [N% confidence]" or "(✗) ... [N% confidence]"
    checkmark_pattern = r"\(([✓✗])\)\s*([^[\n]+)(?:\[(\d+)%\s+confidence\])?"
    for match in re.finditer(checkmark_pattern, output):
        status = match.group(1)
        text = match.group(2).strip()
        conf = int(match.group(3)) / 100 if match.group(3) else (0.7 if status == "✓" else 0.3)
        
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

_FAN_OUT_ALARM_PROMPT = """You are a factuality auditor. Review this reasoning pass output and identify:

1. Claims that lack sufficient grounding in the input
2. Logical jumps without justification
3. Assertions presented as fact that are actually opinion
4. Numerical claims that seem implausible

For each concern, cite the exact text from the pass output and explain why it's problematic.

**PASS OUTPUT:**
{pass_output}

**AUDIT REPORT:**
"""

_CLAIM_EXTRACTION_PROMPT = """Extract all key claims from this reasoning output in JSON format:

{{
  "claims": [
    {{"text": "...", "confidence": 0.8, "category": "inference"}},
    {{"text": "...", "confidence": 0.6, "category": "opinion"}}
  ]
}}

Confidence: 0.0 (speculative) to 1.0 (definite).
Category: fact|inference|opinion|assumption.

**OUTPUT:**
{pass_output}

**JSON:**
"""

_FAN_OUT_SYNTHESIS_PROMPT = """Synthesize these perspectives into a coherent final answer.

Each perspective analyzed the question using different mandates (defense/prosecution/forensics/compliance/red_team/timeline).

**QUESTION:**
{question}

**PERSPECTIVE OUTPUTS:**
{perspective_outputs}

**SYNTHESIS TASK:**
1. Identify converged claims (all perspectives agree)
2. Identify contested claims (perspectives disagree)
3. Resolve contradictions by weighing evidence and mandates
4. Produce a unified answer that acknowledges uncertainty

**FINAL ANSWER:**
"""


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
        enable_research: Enable research tools (Nova search, web search)
        research_query: Optional custom research query
        dama_node_id: DAMA device node ID for telemetry
        dama_metric: DAMA metric name
        web_domain_whitelist: Whitelist domains for web search
    
    Returns:
        Dict with keys: final_answer, pass_outputs, confidence, duration_secs
    """
    import time
    import os
    
    start_time = time.time()
    
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
        task_class = await provider_module.classify_task(
            question,
            provider=provider_config.get("provider", "") if provider_config else ""
        )
    
    # Get task profile
    if task_class not in directives_module.TASK_CLASS_NAMES:
        log.warning(f"Unknown task class '{task_class}'; using 'general'")
        task_class = "general"
    
    task_profile = directives_module.TASK_CLASS_PROFILES[task_class]
    directives = task_profile.get("directives", [])
    
    # Initialize provider config
    if provider_config is None:
        provider_config = {}
    
    # REQUIRED: provider must be explicitly specified (no defaults!)
    if not provider_config.get("provider"):
        raise ValueError("provider is REQUIRED in provider_config. Must be 'anthropic', 'ollama', or other valid provider.")
    
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
    
    # Run safety precheck if required
    if task_profile.get("safety_precheck"):
        safe, reason = await provider_module._run_safety_precheck(question, provider=cfg.provider)
        if not safe:
            log.warning(f"Safety precheck failed: {reason}")
            return {
                "final_answer": f"Request blocked by safety check: {reason}",
                "pass_outputs": [],
                "confidence": 0.0,
                "duration_secs": time.time() - start_time,
            }
    
    pass_outputs = []
    validation_results = []
    
    # Execute passes
    for pass_num in range(1, passes + 1):
        log.info(f"Pass {pass_num}/{passes}")
        
        # Select framing
        if pass_num == passes and len(directives) > 0:
            # Last pass: use final directive
            framing_name, framing_text = directives[-1]
        else:
            validation_data = validation_results[-1] if validation_results else None
            framing_name, framing_text = _select_adaptive_framing(
                pass_num, passes, directives, validation_data
            )
        
        # Construct prompt
        system_prompt = f"""You are an expert reasoner. Apply this framing strictly:

{framing_text}

Use the mandate to structure your response. Be precise and evidence-based."""
        
        user_prompt = f"Question: {question}"
        
        # Select provider and model
        tier = directives_module._FRAMING_TIER.get(framing_name, "medium")
        provider_name = provider_config["provider"]  # Already validated as required above
        model_name = model or provider_module._model_for_tier(cfg, tier, task_class)
        
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
            
            pass_outputs.append(output)
            
            # Extract and validate claims
            claims = _extract_claims_from_pass_output(output)
            validation = await _validate_claims_against_ground_truth(claims, ground_truth_provider)
            validation_results.append(validation)
            
            log.info(f"Pass {pass_num} complete ({framing_name})")
        
        except Exception as e:
            with open("/tmp/deep_think_debug.log", "a") as f:
                f.write(f"Exception in _call_provider: {type(e).__name__}: {str(e)[:200]}\n")
            log.error(f"Pass {pass_num} failed: {e}")
            pass_outputs.append(f"[ERROR: {e}]")
            validation_results.append(None)
    
    # Synthesize final answer
    final_answer = pass_outputs[-1] if pass_outputs else ""
    
    # Calculate confidence
    confidences = [v.overall_confidence for v in validation_results if v]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.5
    
    duration = time.time() - start_time
    
    return {
        "final_answer": final_answer,
        "pass_outputs": pass_outputs,
        "confidence": avg_confidence,
        "duration_secs": duration,
    }


# ---------------------------------------------------------------------------
# Fan-out reasoning (from engine.py lines 2036-2488)
# ---------------------------------------------------------------------------

async def run_fan_out(
    question: str,
    width: int = 3,
    height: int = 2,
    task_class: Optional[str] = None,
    data_policy: str = "any",
    model: Optional[str] = None,
    provider_config: Optional[dict] = None,
    ground_truth_provider: Optional[Any] = None,
    force_local_models: bool = False,
    device_id: str = "",
) -> dict:
    """Fan-out reasoning with multiple perspectives.
    
    Args:
        question: Question to analyze
        width: Number of parallel perspectives (1-6)
        height: Passes per perspective (1-5)
        task_class: Optional task class routing
        data_policy: "any" | "local" | "cloud"
        model: Override model for all tiers
        provider_config: Per-call provider overrides
        ground_truth_provider: Optional ground truth validator
        force_local_models: When True, enforce local-only Ollama, block cloud providers.
                            Used for MQTT operations to prevent data leakage.
        device_id: Device ID for logging. Used to tag MQTT enforcement logs.
    
    Returns:
        Dict with final_answer, perspective_outputs, synthesis, confidence, duration_secs
    """
    import time
    import os
    
    start_time = time.time()
    
    # Check environment override for force_local_models (security gate)
    env_force_local = os.getenv("DEEP_THINK_FORCE_LOCAL", "1") != "0"
    force_local_models = force_local_models or env_force_local
    
    # Check for production security lock (strictest mode)
    ollama_only_mode = os.getenv("OLLAMA_ONLY_MODE", "0") != "0"
    if ollama_only_mode:
        force_local_models = True
    
    # Enforce local-only models on all configs
    if provider_config is None:
        provider_config = {}
    
    # REQUIRED: provider must be explicitly specified (no defaults!)
    if not provider_config.get("provider"):
        raise ValueError("provider is REQUIRED in provider_config. Must be 'anthropic', 'ollama', or other valid provider.")
    
    cfg = provider_module.build_provider_config(provider_config)
    
    if force_local_models:
        await provider_module._validate_and_enforce_local_models(cfg, force_local_models, device_id)
        if device_id:
            log.info(f"[MQTT] Running local-only fan-out for device {device_id}")
    
    # Apply data_policy override
    if force_local_models:
        cfg.data_policy = "local"
        provider_config["data_policy"] = "local"
    elif data_policy and data_policy != "any":
        cfg.data_policy = data_policy
        provider_config["data_policy"] = data_policy
    
    # Auto-classify if not provided
    if not task_class:
        task_class = await provider_module.classify_task(question, provider=cfg.provider)
    
    # Get mandates for this task class
    mandates = directives_module.PERSPECTIVE_MANDATES.get(task_class, {})
    mandate_names = list(mandates.keys())[:width]
    
    if not mandate_names:
        # Fallback: use default investigation mandates
        mandates = directives_module.PERSPECTIVE_MANDATES.get("investigation", {})
        mandate_names = list(mandates.keys())[:width]
    
    log.info(f"Fan-out: {len(mandate_names)} perspectives × {height} passes each")
    
    # Execute perspectives in parallel
    perspective_outputs = {}
    
    async def run_perspective(mandate_name: str) -> tuple[str, dict]:
        """Run one perspective with its mandate."""
        mandate = mandates.get(mandate_name, "")
        
        outputs = []
        for pass_num in range(1, height + 1):
            system_prompt = f"""Adopt this perspective: {mandate_name}

{mandate}

Use this perspective to analyze the question. Be consistent with your assigned viewpoint."""
            
            user_prompt = f"Question: {question}"
            
            try:
                output = await provider_module._call_provider(
                    provider=provider_config["provider"],  # Already validated as required above
                    model=model or "qwen3.5:27b",
                    system=system_prompt,
                    user_prompt=user_prompt,
                    tier="medium",
                    provider_config=provider_config,
                )
                outputs.append(output)
            except Exception as e:
                log.error(f"Perspective {mandate_name} pass {pass_num} failed: {e}")
                outputs.append(f"[ERROR: {e}]")
        
        # Synthesize perspective
        perspective_synthesis = "\n\n".join(outputs)
        return (mandate_name, {"outputs": outputs, "synthesis": perspective_synthesis})
    
    # Run all perspectives concurrently
    tasks = [run_perspective(name) for name in mandate_names]
    results = await asyncio.gather(*tasks)
    
    for name, output in results:
        perspective_outputs[name] = output
    
    # Synthesize across perspectives
    perspective_texts = "\n\n---\n\n".join([
        f"[{name}]\n{output['synthesis']}"
        for name, output in perspective_outputs.items()
    ])
    
    synthesis_prompt = _FAN_OUT_SYNTHESIS_PROMPT.format(
        question=question,
        perspective_outputs=perspective_texts,
    )
    
    try:
        final_answer = await provider_module._call_provider(
            provider=provider_config["provider"],  # Already validated as required above
            model=model or "llama3.1:8b",
            system="You are a synthesis expert. Integrate all perspectives into a coherent answer.",
            user_prompt=synthesis_prompt,
            tier="heavy",
            provider_config=provider_config,
        )
    except Exception as e:
        log.error(f"Synthesis failed: {e}")
        final_answer = f"[SYNTHESIS ERROR: {e}]"
    
    duration = time.time() - start_time
    
    return {
        "final_answer": final_answer,
        "perspective_outputs": perspective_outputs,
        "synthesis": final_answer,
        "confidence": 0.7,  # Placeholder
        "duration_secs": duration,
    }
