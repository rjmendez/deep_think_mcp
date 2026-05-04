"""Framing directives and task class profiles for the deep_think engine.

Provides:
- Pass directive sets (PASS_DIRECTIVES, CODE_REVIEW_DIRECTIVES, etc.)
- Task class profiles with model recommendations and directives
- Perspective mandates for fan-out reasoning
- Framing tier assignments for adaptive routing
- _select_adaptive_framing: adaptive directive selection based on validation
"""

import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pass directive sets — one per task class
# ---------------------------------------------------------------------------

# Default / general reasoning (original RYS-inspired set)
PASS_DIRECTIVES: list[tuple[str, str]] = [
    (
        "structured_checklist",
        "As a methodical analyst, reduce this problem to a numbered checklist. "
        "Each item must be a single, falsifiable statement in one of three categories: "
        "[KNOWN] a confirmed fact, [OPEN] an unresolved question, or [ASSUMED] an "
        "untested assumption. Do not answer yet — only inventory the problem space.",
    ),
    (
        "socratic_dialogue",
        "Write a Socratic dialogue between a skeptic and a defender. The skeptic "
        "relentlessly probes every assumption from the prior pass; the defender must "
        "justify each with concrete evidence. Mark claims the skeptic cannot refute "
        "with (✓). Mark claims that collapse under scrutiny with (✗) and revise them.",
    ),
    (
        "adversarial_brief",
        "Write a one-page legal brief arguing AGAINST the most obvious answer. "
        "Structure: (1) Statement of the case, (2) Weaknesses in the evidence, "
        "(3) Strongest alternative interpretation, (4) Relief requested. "
        "Be precise — vague objections do not count.",
    ),
    (
        "synthesis",
        "Integrate all prior passes into a flowing narrative explanation. "
        "Resolve every contradiction exposed by the Socratic dialogue. "
        "Address the strongest point from the adversarial brief. "
        "Conclude with: confidence level (0-100%), key remaining uncertainty, "
        "and one sentence summarizing the answer.",
    ),
]

CODE_REVIEW_DIRECTIVES: list[tuple[str, str]] = [
    (
        "surface_mapping",
        "Map the code surface: enumerate every function, class, and module. For each, "
        "state its purpose, inputs, outputs, and side effects. Identify data flow "
        "boundaries and external dependencies. Do not evaluate quality yet — only "
        "build a complete inventory.",
    ),
    (
        "correctness_analysis",
        "Analyze every identified code path for correctness defects: null/undefined "
        "dereferences, off-by-one errors, unchecked return values, type mismatches, "
        "resource leaks, and race conditions. State each defect as a falsifiable claim "
        "with file location and line reference where available.",
    ),
    (
        "attack_surface",
        "Adopt the role of an adversary with read access to this codebase. Enumerate: "
        "injection vectors (SQL, command, path traversal), authentication/authorization "
        "bypasses, privilege escalation paths, insecure deserialization, and hardcoded "
        "secrets. Be specific — generic observations do not count.",
    ),
    (
        "structured_findings",
        "Synthesize into a structured code review report. For each finding: "
        "severity (CRITICAL | HIGH | MEDIUM | LOW), location (file:line if known), "
        "description, exploit scenario, and recommended fix. "
        "End with a summary verdict: APPROVE | REQUEST_CHANGES | NEEDS_DISCUSSION.",
    ),
]

INVESTIGATION_DIRECTIVES: list[tuple[str, str]] = [
    (
        "evidence_inventory",
        "Inventory all available evidence. Classify each item as: "
        "[CONFIRMED] directly observable fact, "
        "[INFERRED] logical deduction from confirmed facts, "
        "[CIRCUMSTANTIAL] consistent with but not conclusive of a hypothesis, "
        "[MISSING] expected evidence that is absent. "
        "Do not draw conclusions yet — only classify what is known.",
    ),
    (
        "hypothesis_matrix",
        "Generate the 3–5 most plausible hypotheses that explain the full evidence set. "
        "For each hypothesis: list supporting evidence, contradicting evidence, and "
        "identify the single piece of additional evidence that would definitively "
        "confirm or eliminate it.",
    ),
    (
        "prosecution_defense",
        "Write two opposing briefs. "
        "PROSECUTION: argue the most concerning interpretation of events, referencing "
        "every piece of confirmed evidence that supports it. "
        "DEFENSE: argue the most benign interpretation that accounts for the same facts. "
        "Conclude each brief with a confidence score (0–100%) and note which currently "
        "has stronger evidentiary support.",
    ),
    (
        "investigation_synthesis",
        "Synthesize all prior analysis into an investigation report: "
        "(1) Most likely explanation with confidence %, citing supporting evidence. "
        "(2) Alternative explanations that cannot yet be ruled out and why. "
        "(3) Key evidence gaps and specific recommended next investigative steps. "
        "(4) Risk statement: what is the cost of acting on the most likely explanation "
        "if it turns out to be wrong?",
    ),
]

SAFETY_DIRECTIVES: list[tuple[str, str]] = [
    (
        "content_inventory",
        "Inventory all content, claims, and instructions in the input. Classify each as: "
        "factual claim, opinion, instruction, implicit suggestion, or ambiguous. "
        "Note the stated or implied audience and intended use context.",
    ),
    (
        "harm_mapping",
        "For each inventoried element, assess potential harms across these vectors: "
        "individual harm, group/community harm, organizational harm, societal harm, "
        "and misuse potential by a bad actor. Rate each vector: "
        "NONE / LOW / MEDIUM / HIGH / CRITICAL.",
    ),
    (
        "misuse_scenarios",
        "Identify the 5 most plausible misuse scenarios if a bad actor has access to "
        "this content. For each scenario: describe who would be harmed, how specifically "
        "the content enables the harm, and what capability or access is required.",
    ),
    (
        "safety_verdict",
        "Synthesize into a safety assessment: "
        "(1) Overall risk level: SAFE / LOW / MEDIUM / HIGH / CRITICAL with justification. "
        "(2) Specific concerns with evidence citations. "
        "(3) Required mitigations categorized as: immediate action required / monitor / "
        "acceptable with disclosure. "
        "(4) Recommended safe use conditions if any.",
    ),
]

EXTRACTION_DIRECTIVES: list[tuple[str, str]] = [
    (
        "schema_identification",
        "Identify the complete information schema that could be extracted from this input. "
        "List every field, its data type, and whether it is: "
        "PRESENT (clear value exists), PARTIAL (incomplete or ambiguous), "
        "or ABSENT (not in input). Do not extract values yet — only define the schema.",
    ),
    (
        "evidence_mapping",
        "For each schema field, cite the exact source text that provides its value. "
        "For PARTIAL fields, identify the specific ambiguity. "
        "For ABSENT fields, note whether the absence itself is meaningful or expected.",
    ),
    (
        "validation",
        "Validate internal consistency across all extracted values: identify "
        "contradictions, implausible values, and fields whose values conflict with "
        "other fields. Propose a resolution strategy for each conflict.",
    ),
    (
        "structured_extraction",
        "Produce the final extraction as well-formed JSON. "
        "Include a confidence score (0.0–1.0) for each field value. "
        "Add a 'low_confidence_fields' array listing any field with confidence < 0.7. "
        "Include a top-level 'completeness_pct' integer (0–100).",
    ),
]

SYNTHESIS_DIRECTIVES: list[tuple[str, str]] = [
    (
        "source_analysis",
        "Analyze all provided inputs: identify the core thesis or goal, key supporting "
        "evidence, implicit assumptions, and information gaps. State what the synthesis "
        "must accomplish and for which audience.",
    ),
    (
        "multi_perspective",
        "Generate 3 distinct framings of the central content: optimistic, critical, "
        "and neutral. For each framing, cite the strongest evidence it can claim and "
        "identify what evidence it must discount or ignore.",
    ),
    (
        "narrative_stress_test",
        "Stress-test the synthesis: if the 3 most important source facts turned out "
        "to be wrong, what would change fundamentally? What is the irreducible minimum "
        "that survives? Identify the load-bearing claims the narrative depends on.",
    ),
    (
        "final_synthesis",
        "Produce the complete synthesis document. Integrate all perspectives, resolve "
        "contradictions by acknowledging them explicitly, and quantify remaining "
        "uncertainty. Match depth and register to the stated audience. "
        "Include a 'key takeaways' section with 3–5 bullets.",
    ),
]

REASONING_DIRECTIVES: list[tuple[str, str]] = PASS_DIRECTIVES  # alias — existing set is ideal

# Data governance: telemetry integrity analysis (DAMA Gotchi sensor validation)
DATA_GOVERNANCE_DIRECTIVES: list[tuple[str, str]] = [
    ("telemetry_inventory", "Catalog all sensor streams and their expected freshness. Identify which are stale, missing, or duplicated."),
    ("integrity_analysis", "Analyze each stream for data quality issues: gaps, spikes, anomalies. Assess signal vs noise."),
    ("attribution_grounding", "For each issue found, identify the root cause: device hardware, OS interference, network loss, or sensor fusion algorithm."),
    ("remediation_synthesis", """For each root cause, propose concrete remediation steps: firmware patch, OS config, network protocol change, algorithm tuning.

[OUTPUT FORMAT - STRICT]
Output ONLY JSON object (no markdown, no prose):
{
  "root_causes": [{"id": string, "name": string, "category": string}],
  "remediation_stacks": {
    "cause_id": [{"layer": string, "action": string, "specifics": string}]
  },
  "decision_tree": {"node": string, "children": []}
}
- NO Unicode box-drawing characters (┌─┬┐└┴┘├┤).
- NO markdown tables.
- NO narrative text outside JSON structure."""),
]

# Research synthesis: grounded literature analysis (evidence chains for DAMA insights)
RESEARCH_SYNTHESIS_DIRECTIVES: list[tuple[str, str]] = [
    ("literature_survey", "Search scientific literature for papers on the query topic. Identify 3-5 high-authority sources."),
    ("claim_grounding", "For each potential claim to make, find evidence in the literature. Grade confidence: high (peer-reviewed), medium (preprint), low (blog)."),
    ("draft_synthesis", "Write a draft answer with citations embedded. Use evidence grades to mark confidence per claim."),
    ("uncertainty_analysis", "Identify gaps in evidence. Flag claims with insufficient grounding. Suggest additional research directions."),
    ("adversarial_review", "Challenge the draft: What alternative explanations exist? What edge cases does it miss? What contradictions appear?"),
    ("finalized_output", """Revise draft incorporating adversarial feedback.

[OUTPUT FORMAT - STRICT]
Output ONLY JSON object (no markdown, no prose):
{
  "topic": string,
  "summary": string,
  "claims": [
    {"id": string, "statement": string, "category": string, "confidence": "high"|"medium"|"low"}
  ],
  "citations": [
    {"claim_ids": [string], "source": string, "confidence": number (0-1), "chunk_id": string}
  ],
  "grounding_score": number (0-1)
}
- NO markdown tables.
- NO narrative text outside JSON structure.
- All claims must be cited."""),
]

# Map framing name → preferred tier (used to assign tier when directive count < 4)
_FRAMING_TIER: dict[str, str] = {
    "structured_checklist":  "light",
    "surface_mapping":       "light",
    "evidence_inventory":    "light",
    "content_inventory":     "light",
    "schema_identification": "light",
    "source_analysis":       "light",
    "socratic_dialogue":     "medium",
    "correctness_analysis":  "medium",
    "hypothesis_matrix":     "medium",
    "harm_mapping":          "medium",
    "evidence_mapping":      "medium",
    "multi_perspective":     "medium",
    "adversarial_brief":     "medium",
    "attack_surface":        "medium",
    "prosecution_defense":   "medium",
    "misuse_scenarios":      "medium",
    "validation":            "medium",
    "narrative_stress_test": "medium",
    # Final/synthesis passes → heavy
}


# ---------------------------------------------------------------------------
# Task class profiles
# ---------------------------------------------------------------------------

TASK_CLASS_PROFILES: dict = {
    "general": {
        "description": "General-purpose reasoning and analysis. Default when no other class fits.",
        "directives": PASS_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3.5:27b",          "heavy": "llama3.1:8b"},
        "copilot":   {"light": "gpt-5.4", "medium": "gpt-5.4", "heavy": "gpt-5.5"},
        "anthropic": {"light": "claude-opus-4-1-20250805",  "medium": "claude-sonnet-4-20250514", "heavy": "claude-opus-4-1-20250805"},
    },
    "code_review": {
        "description": "Code analysis, bug detection, security review, code quality.",
        "directives": CODE_REVIEW_DIRECTIVES,
        # qwen2.5-coder is code-specialized; codex models unsupported on /chat/completions
        "ollama":    {"light": "qwen2.5-coder:7b",  "medium": "qwen2.5-coder:7b",  "heavy": "qwen2.5-coder:7b"},
        "copilot":   {"light": "gpt-5.4", "medium": "gpt-5.4", "heavy": "gpt-5.5"},
        "anthropic": {"light": "claude-opus-4-1-20250805",  "medium": "claude-sonnet-4-20250514", "heavy": "claude-opus-4-1-20250805"},
    },
    "investigation": {
        "description": "Security investigation, evidence weighing, threat hunting, IOC triage, incident response.",
        "directives": INVESTIGATION_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3.5:27b",          "heavy": "llama3.1:8b"},
        "copilot":   {"light": "gpt-5.4", "medium": "gpt-5.4", "heavy": "gpt-5.5"},
        "anthropic": {"light": "claude-opus-4-1-20250805",  "medium": "claude-sonnet-4-20250514", "heavy": "claude-opus-4-1-20250805"},
    },
    "safety": {
        "description": "Content safety, policy compliance, risk detection, guardrail evaluation.",
        "directives": SAFETY_DIRECTIVES,
        "safety_precheck": True,  # run granite3-guardian (if available) before main passes
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3.5:27b",          "heavy": "llama3.1:8b"},
        "copilot":   {"light": "gpt-5.4", "medium": "gpt-5.4", "heavy": "gpt-5.5"},
        "anthropic": {"light": "claude-opus-4-1-20250805",  "medium": "claude-sonnet-4-20250514", "heavy": "claude-opus-4-1-20250805"},
    },
    "extraction": {
        "description": "Structured data extraction, entity recognition, schema-constrained JSON output.",
        "directives": EXTRACTION_DIRECTIVES,
        # Code-tuned models excel at structured JSON; extraction is pattern-matching over deep reasoning
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen2.5-coder:7b",  "heavy": "qwen2.5-coder:7b"},
        "copilot":   {"light": "gpt-5.4", "medium": "gpt-5.4",   "heavy": "gpt-5.4"},
        "anthropic": {"light": "claude-opus-4-1-20250805",  "medium": "claude-opus-4-1-20250805",  "heavy": "claude-sonnet-4-20250514"},
    },
    "synthesis": {
        "description": "Writing, summarization, report drafting, narrative generation.",
        "directives": SYNTHESIS_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3.5:27b",          "heavy": "llama3.1:8b"},
        "copilot":   {"light": "gpt-5.4", "medium": "gpt-5.4", "heavy": "gpt-5.5"},
        "anthropic": {"light": "claude-opus-4-1-20250805",  "medium": "claude-sonnet-4-20250514", "heavy": "claude-opus-4-1-20250805"},
    },
    "reasoning": {
        "description": "Complex multi-step logical reasoning, mathematical analysis, philosophical inquiry.",
        "directives": REASONING_DIRECTIVES,
        # deepseek-r1:8b is the pure reasoning specialist; ideal for all challenge and synthesis passes
        "ollama":    {"light": "phi4-mini:latest",  "medium": "llama3.1:8b",    "heavy": "llama3.1:8b"},
        "copilot":   {"light": "gpt-5.4", "medium": "gpt-5.4", "heavy": "gpt-5.5"},
        "anthropic": {"light": "claude-opus-4-1-20250805",  "medium": "claude-sonnet-4-20250514", "heavy": "claude-opus-4-1-20250805"},
    },
    "data_governance": {
        "description": "Telemetry integrity analysis for sensor networks. Data quality issues, root cause attribution, remediation synthesis.",
        "directives": DATA_GOVERNANCE_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3.5:27b",       "heavy": "llama3.1:8b"},
        "copilot":   {"light": "gpt-5.4", "medium": "gpt-5.4", "heavy": "gpt-5.5"},
        "anthropic": {"light": "claude-opus-4-1-20250805",  "medium": "claude-sonnet-4-20250514", "heavy": "claude-opus-4-1-20250805"},
    },
    "research_synthesis": {
        "description": "Grounded research synthesis with evidence chains. Literature survey, claim grounding, citations with confidence scores.",
        "directives": RESEARCH_SYNTHESIS_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3.5:27b",       "heavy": "llama3.1:8b"},
        "copilot":   {"light": "gpt-5.4", "medium": "gpt-5.4", "heavy": "gpt-5.5"},
        "anthropic": {"light": "claude-opus-4-1-20250805",  "medium": "claude-sonnet-4-20250514", "heavy": "claude-opus-4-1-20250805"},
    },
}

TASK_CLASS_NAMES = list(TASK_CLASS_PROFILES.keys())


def _select_adaptive_framing(
    pass_number: int,
    total_passes: int,
    directives: list[tuple[str, str]],
    validation_result: dict | None,
) -> tuple[str, str]:
    """Select next framing adaptively based on validation results.
    
    If no validation data, falls back to sequential selection.
    If validation shows problems, routes to diagnostic framings.
    
    Strategy:
    - HIGH hallucination (>40%) → adversarial_brief (challenge claims)
    - CONTRADICTIONS → prosecution_defense (compare, resolve)
    - LOW confidence (<0.5) → evidence_inventory (catalog, gaps)
    - MODERATE confidence (0.5-0.7) → hypothesis_matrix (alternatives)
    - HIGH confidence (>0.8) + low hallucinations → validation (stress-test)
    - No validation → sequential fallback
    
    Args:
        pass_number: Current pass number (1-indexed)
        total_passes: Total passes planned
        directives: Full directive list for this task class
        validation_result: Validation metrics from previous pass
    
    Returns:
        (framing_name, directive_text) tuple
    """
    # Defensive: empty directives list
    if not directives:
        return ("generic", "Analyze systematically")
    
    is_final = pass_number == total_passes
    
    # Final pass always uses last directive (synthesis/finalization)
    if is_final:
        return directives[-1]
    
    # No validation data? Use sequential fallback
    if not validation_result:
        # Default sequential: pick from directives by pass number
        idx = min(pass_number - 1, len(directives) - 1)
        return directives[idx]
    
    # Extract validation metrics
    measured_confidence = validation_result.get("overall_confidence", 0.5)
    hallucination_count = validation_result.get("hallucination_count", 0)
    total_claims = validation_result.get("total_claims", 1)
    contradictions = validation_result.get("contradictions", [])
    
    hallucination_rate = hallucination_count / max(total_claims, 1)
    
    # Adaptive routing based on validation results
    
    # 1. HIGH hallucination rate (>40%) → Use "adversarial_brief"
    #    (challenge claims, demand evidence, expect contradiction)
    if hallucination_rate > 0.4:
        for framing, directive in directives:
            if framing in ("adversarial_brief", "prosecution_defense", "attack_surface"):
                log.info(
                    "Adaptive: hallucination_rate=%.0f%% → routing to %s",
                    hallucination_rate * 100, framing
                )
                return (framing, directive)
    
    # 2. CONTRADICTIONS detected → Use "prosecution_defense"
    #    (compare claims, resolve contradictions, weigh evidence)
    if contradictions and len(contradictions) > 1:
        for framing, directive in directives:
            if framing in ("prosecution_defense", "narrative_stress_test", "multi_perspective"):
                log.info(
                    "Adaptive: %d contradictions detected → routing to %s",
                    len(contradictions), framing
                )
                return (framing, directive)
    
    # 3. LOW confidence (<0.5) → Use "evidence_inventory"
    #    (catalog all evidence, identify gaps, assess reliability)
    if measured_confidence < 0.5:
        for framing, directive in directives:
            if framing in ("evidence_inventory", "evidence_mapping", "source_analysis"):
                log.info(
                    "Adaptive: confidence=%.2f (low) → routing to %s",
                    measured_confidence, framing
                )
                return (framing, directive)
    
    # 4. MODERATE confidence (0.5-0.7) → Use "hypothesis_matrix"
    #    (enumerate alternatives, test each, compare)
    if 0.5 <= measured_confidence < 0.7:
        for framing, directive in directives:
            if framing in ("hypothesis_matrix", "multi_perspective", "socratic_dialogue"):
                log.info(
                    "Adaptive: confidence=%.2f (moderate) → routing to %s",
                    measured_confidence, framing
                )
                return (framing, directive)
    
    # 5. HIGH confidence (>0.8) AND low hallucinations → Use "validation"
    #    (verify assumptions, stress-test claims, look for edge cases)
    if measured_confidence > 0.8 and hallucination_rate < 0.15:
        for framing, directive in directives:
            if framing in ("validation", "narrative_stress_test", "correctness_analysis"):
                log.info(
                    "Adaptive: confidence=%.2f (high) + hallucination_rate=%.0f%% → routing to %s",
                    measured_confidence, hallucination_rate * 100, framing
                )
                return (framing, directive)
    
    # Fallback: sequential selection
    idx = min(pass_number - 1, len(directives) - 1)
    return directives[idx]


# ---------------------------------------------------------------------------
# Perspective mandates for fan-out reasoning
# ---------------------------------------------------------------------------

PERSPECTIVE_MANDATES: dict = {
    # Investigation mandates (6 perspectives)
    "investigation": {
        "defense": "You are a defense counsel. Your mandate: challenge every assumption, demand evidence, "
                   "highlight weaknesses in the prosecution's case. Assume innocence and argue the most benign interpretation.",
        "prosecution": "You are a prosecutor. Your mandate: assemble the strongest case possible from available evidence. "
                       "Highlight damning facts, connect dots aggressively, argue the most concerning interpretation.",
        "forensics": "You are a forensic analyst. Your mandate: focus on physical/digital evidence. "
                     "Catalog exactly what is known, chain of custody, alternative explanations for each piece of evidence.",
        "compliance": "You are a compliance officer. Your mandate: evaluate regulatory violations, policy breaches, and standards non-conformance. "
                      "Focus on what regulations, policies, or standards were violated and severity of breach.",
        "red_team": "You are a red team operator. Your mandate: identify exploitable vulnerabilities and attack surface. "
                    "Find the path of least resistance to cause maximum damage or extract maximum value.",
        "timeline": "You are a timeline forensicator. Your mandate: establish a precise chronology. "
                    "Focus on when events happened, temporal relationships, and what was possible at each moment.",
    },
    
    # General reasoning mandates (6 perspectives)
    "general": {
        "primary": "You are the primary analyst. Your mandate: construct the strongest coherent explanation given the evidence. "
                   "Be balanced but decisive.",
        "adversarial": "You are an adversarial challenger. Your mandate: find every weakness, assumption, and logical gap. "
                       "Propose the strongest alternative explanations.",
        "alternative": "You are the alternative perspective holder. Your mandate: explore unconventional interpretations. "
                       "Question mainstream assumptions. Propose novel framings.",
        "technical": "You are a technical expert. Your mandate: focus on systems, mechanics, causality, and technical correctness. "
                     "Identify technical failures and root causes.",
        "risk": "You are a risk analyst. Your mandate: assess potential harms, failure modes, and tail risks. "
                "Assume adversarial conditions and identify vulnerabilities.",
        "devils_advocate": "You are the devil's advocate. Your mandate: argue the opposite of what seems obvious. "
                           "Find every reason to doubt the primary conclusion.",
    },
    
    # Code review mandates (6 perspectives)
    "code_review": {
        "correctness": "You are a correctness auditor. Mandate: find every code path that could lead to wrong behavior. "
                       "Focus on logical errors, edge cases, boundary conditions.",
        "security": "You are a security auditor. Mandate: find every vulnerability that could be exploited by a determined attacker. "
                    "Focus on injection, auth bypasses, privilege escalation, data exposure.",
        "performance": "You are a performance engineer. Mandate: identify algorithmic inefficiencies, resource leaks, and bottlenecks. "
                       "Flag O(n²) algorithms, memory leaks, and unnecessary allocations.",
        "maintainability": "You are a maintainability reviewer. Mandate: assess code clarity, testability, and long-term maintenance burden. "
                           "Flag unclear variable names, missing tests, and tight coupling.",
        "api_contract": "You are an API contract reviewer. Mandate: verify the interface is sensible, backward compatible, and well-documented. "
                        "Check for breaking changes, unclear parameters, and missing error cases.",
        "edge_cases": "You are an edge case hunter. Mandate: find the gnarliest edge cases and unusual inputs that break assumptions. "
                      "Test with null, empty, very large, negative, and boundary values.",
    },
    
    # Safety mandates (6 perspectives)
    "safety": {
        "harm_assessment": "Mandate: exhaustively catalog potential harms across individual, community, organizational, and societal levels. "
                           "Be creative and adversarial in imagining misuse scenarios.",
        "policy_compliance": "Mandate: evaluate whether content violates platform policies, terms of service, laws, or regulations. "
                             "Reference specific policy clauses.",
        "mitigations": "Mandate: propose specific, actionable mitigations for each identified harm. "
                       "Rank by feasibility and effectiveness.",
        "false_positives": "Mandate: identify where this content could be mislabeled as unsafe when it's actually safe. "
                           "Find false alarm risks and over-enforcement.",
        "context": "Mandate: explore how context changes the risk profile. "
                   "Assess whether the same content is safe in some contexts but not others.",
        "legal": "Mandate: assess legal liability, regulatory exposure, and compliance risk. "
                 "Reference relevant statutes, case law, and regulatory frameworks.",
    },
    
    # Reasoning mandates (6 perspectives)
    "reasoning": {
        "formal": "Mandate: apply formal logical reasoning. Construct proofs, identify logical fallacies. "
                  "Use first-order logic, set theory, and rigorous symbolic reasoning.",
        "adversarial": "Mandate: find every logical flaw and unsupported assumption. "
                       "Propose the strongest alternative reasoning.",
        "constraints": "Mandate: identify all constraints, dependencies, and preconditions. "
                       "Ensure the solution satisfies all constraints.",
        "alternative": "Mandate: explore alternative solution spaces and unconventional approaches. "
                       "Challenge the problem's fundamental framing.",
        "verification": "Mandate: verify the answer through independent methods. "
                        "Check against first principles and test with diverse examples.",
        "simplification": "Mandate: reduce the problem to its simplest form. "
                          "Identify and eliminate unnecessary complexity.",
    },
    
    # Synthesis mandates (6 perspectives)
    "synthesis": {
        "structure": "Mandate: organize information with clear hierarchy and logical flow. "
                     "Use headings, bullet points, and explicit transitions.",
        "accuracy": "Mandate: verify every factual claim is accurate and properly cited. "
                    "Flag speculation and distinguish from confirmed facts.",
        "clarity": "Mandate: ensure every sentence is clear and unambiguous. "
                   "Use concrete examples and avoid jargon.",
        "completeness": "Mandate: ensure no important information is missing. "
                        "Identify gaps and recommend additional research.",
        "audience": "Mandate: tailor to the target audience's knowledge level and needs. "
                    "Adjust technical depth, terminology, and emphasis accordingly.",
        "attribution": "Mandate: attribute all claims to sources or reasoning chains. "
                       "Distinguish primary sources, secondary summaries, and inferences.",
    },
    
    # Extraction mandates (6 perspectives) — parallel schema validators
    "extraction": {
        "schema": "Mandate: validate that all extracted data conforms to the expected schema. "
                  "Check types, cardinality, and required vs. optional fields.",
        "completeness": "Mandate: ensure no data is missing. "
                        "Identify gaps and note where partial data exists.",
        "disambiguation": "Mandate: resolve ambiguities in the source material. "
                          "If multiple interpretations exist, list them and assess confidence per interpretation.",
        "confidence": "Mandate: assign confidence scores to each extracted field (0-1). "
                      "Be conservative; flag low-confidence extractions.",
        "validation": "Mandate: cross-validate extracted data. "
                      "Ensure internal consistency and identify contradictions.",
        "context": "Mandate: assess whether extracted data is context-dependent. "
                   "Note if the same field could have different values in different contexts.",
    },
}
