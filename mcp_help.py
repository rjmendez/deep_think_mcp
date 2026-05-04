"""MCP Help system — documentation and introspection utilities.

This module centralizes help documentation for all MCP endpoints, making it
easy to maintain and extend command documentation consistently.

Provides:
- Help content for all MCP commands (verify, reason, review, escalate)
- Examples with request/response formats
- Common mistakes and best practices
- Tip lists for optimization
"""

import logging
from typing import Dict, Any, List

log = logging.getLogger(__name__)


# Help documentation for all commands
HELP_DOCS: Dict[str, Dict[str, Any]] = {
    "verify": {
        "description": "Verify a claim using chain-of-thought reasoning with cloud or local LLMs.",
        "usage": "POST /verify with {\"claim\": \"...\", \"provider\": \"cloud|local\", \"context\": \"...\"}",
        "example": {
            "request": {
                "claim": "Python is a compiled language",
                "context": "Programming languages",
                "provider": "cloud"
            },
            "response": {
                "job_id": "uuid-string",
                "status_url": "/verify-status/uuid-string"
            }
        },
        "common_mistakes": [
            "Missing 'claim' field (required)",
            "Using invalid provider (must be 'cloud' or 'local')",
            "Not providing context for complex claims",
            "Polling status too frequently (recommended: 1-2s interval)"
        ],
        "tips": [
            "Provide context for grounded verification",
            "Use cloud provider for higher accuracy, local for privacy",
            "Cache results for identical claims",
        ]
    },
    "reason": {
        "description": "Run multi-pass reasoning with different framings and models.",
        "usage": "POST /call/deep_think_async with {\"question\": \"...\", \"passes\": 2-6, \"task_class\": \"...\"}",
        "example": {
            "request": {
                "question": "How should I optimize this database query?",
                "passes": 3,
                "task_class": "code_review"
            },
            "response": {
                "job_id": "uuid-string",
                "status": "queued"
            }
        },
        "common_mistakes": [
            "Using passes < 2 or > 6 (clamped to range)",
            "Using invalid task_class (check /capabilities for valid options)",
            "Not polling for results (jobs run asynchronously)",
            "Assuming results available immediately (typical latency: 15-180s)"
        ],
        "tips": [
            "Use 2-3 passes for quick analysis, 4-6 for deep investigation",
            "Match task_class to question type (code_review, investigation, etc.)",
            "Enable verify=True for critical decisions requiring extra validation",
            "Use provider_config to specify models or Ollama endpoint"
        ]
    },
    "review": {
        "description": "Perform code review using code_review task class with security focus.",
        "usage": "POST /call/deep_think_async with {\"question\": \"<code_snippet>\", \"task_class\": \"code_review\"}",
        "example": {
            "request": {
                "question": "def authenticate(password): return len(password) > 0",
                "task_class": "code_review",
                "passes": 3
            },
            "response": {
                "job_id": "uuid-string"
            }
        },
        "common_mistakes": [
            "Not using task_class='code_review' (this enables code specialization)",
            "Including too much context (keep focused on review target)",
            "Using too few passes (3+ recommended for thorough review)",
            "Not enabling verify=True for security-critical code"
        ],
        "tips": [
            "Use code_review task_class for specialized code analysis",
            "Enable verify=True for security review",
            "Provide minimal but sufficient context",
            "Use 4-6 passes for security-critical code",
            "Check /capabilities to see code-specialized models in use"
        ]
    },
    "escalate": {
        "description": "Escalate unresolved claims to manual review or higher-tier models.",
        "usage": "Enable verify=True in deep_think_async call, or POST to /verification/escalate",
        "example": {
            "request": {
                "claim": "Unresolved claim from reasoning",
                "reason": "Confidence too low"
            },
            "response": {
                "escalation_id": "uuid-string",
                "status": "escalated"
            }
        },
        "common_mistakes": [
            "Not enabling verify=True when certainty is critical",
            "Escalating without trying local reasoning first",
            "Assuming escalation = guaranteed correctness"
        ],
        "tips": [
            "Enable verify=True in reasoning calls for critical decisions",
            "Use escalation for confidence scores < 0.7",
            "Combine with heavy-tier models for difficult claims",
            "Check escalation_status for escalated items"
        ]
    }
}


def get_help(command: str) -> Dict[str, Any]:
    """Get help documentation for a command.
    
    Args:
        command: The command name (e.g., 'verify', 'reason', 'review', 'escalate')
    
    Returns:
        Dict with description, usage, example, common_mistakes, tips
        or empty dict if command not found
    
    Raises:
        KeyError if command is not recognized
    """
    if command not in HELP_DOCS:
        raise KeyError(f"Unknown command: {command}")
    return HELP_DOCS[command]


def get_all_commands() -> List[str]:
    """Get list of all available help commands.
    
    Returns:
        List of command names
    """
    return sorted(HELP_DOCS.keys())


def has_command(command: str) -> bool:
    """Check if a command has help documentation.
    
    Args:
        command: The command name to check
    
    Returns:
        True if command exists in help docs, False otherwise
    """
    return command in HELP_DOCS


# Metadata for health hints generation
HEALTH_HINTS_CONFIG = {
    "queue_depth_high_threshold": 50,
    "queue_depth_high_hint": "Queue depth is high (>50). Consider increasing VERIFY_MAX_CONCURRENCY.",
    
    "latency_high_threshold": 45,
    "latency_high_hint": "Average latency is high (>45s). Consider using provider=local instead of cloud.",
    
    "failure_rate_high_threshold": 10,
    "failure_rate_high_hint": "Job failure rate is high (>10%). Check ANTHROPIC_API_KEY validity or Ollama connection.",
    
    "completion_rate_low_threshold": 80,
    "completion_rate_low_hint_template": "Only {completion_rate}% of jobs completed successfully. Review verification provider configuration.",
    
    "healthy_hint": "System operating normally"
}


def generate_hints(metrics: Dict[str, Any]) -> List[str]:
    """Generate actionable hints based on queue metrics.
    
    Args:
        metrics: Dict with queue_depth, processing, completed, failed, 
                avg_latency, completion_rate
    
    Returns:
        List of actionable hint strings
    """
    hints = []
    cfg = HEALTH_HINTS_CONFIG
    
    queue_depth = metrics.get("queue_depth", 0)
    avg_latency = metrics.get("avg_latency")
    failed = metrics.get("failed", 0)
    completed = metrics.get("completed", 0)
    completion_rate = metrics.get("completion_rate", 0)
    
    # Check queue depth
    if queue_depth > cfg["queue_depth_high_threshold"]:
        hints.append(cfg["queue_depth_high_hint"])
    
    # Check latency
    if avg_latency and avg_latency > cfg["latency_high_threshold"]:
        hints.append(cfg["latency_high_hint"])
    
    # Check failure rate
    if failed > 0 and completed > 0:
        fail_rate = (failed / (failed + completed)) * 100
        if fail_rate > cfg["failure_rate_high_threshold"]:
            hints.append(cfg["failure_rate_high_hint"])
    
    # Check completion rate
    if completion_rate < cfg["completion_rate_low_threshold"] and (completed + failed) > 10:
        hints.append(cfg["completion_rate_low_hint_template"].format(completion_rate=completion_rate))
    
    # Add positive hint if healthy
    if not hints:
        hints.append(cfg["healthy_hint"])
    
    return hints


# Metadata for capabilities endpoint
CAPABILITIES_CONFIG = {
    "passes": [2, 3, 4, 5, 6],
    "width_range": [1, 2, 3, 4, 5, 6],
    "latency_estimates": {
        "2_passes_cloud": "15-30s",
        "3_passes_cloud": "30-60s",
        "4_passes_cloud": "60-90s",
        "5_passes_cloud": "90-120s",
        "6_passes_cloud": "120-180s",
        "2_passes_local": "10-20s",
        "3_passes_local": "20-40s",
        "4_passes_local": "40-60s",
        "5_passes_local": "60-80s",
        "6_passes_local": "80-120s",
        "fan_out_3x2": "60-120s (3 perspectives × 2 passes)",
    }
}


# Metadata for suggest endpoint
SUGGEST_CONFIG = {
    "complexity_thresholds": {
        "simple": (0, 100),      # < 100 chars
        "moderate": (100, 300),  # 100-300 chars
        "complex": (300, 800),   # 300-800 chars
        "very_complex": (800, float('inf'))  # >= 800 chars
    },
    "passes_by_complexity": {
        "simple": 2,
        "moderate": 3,
        "complex": 4,
        "very_complex": 5,
    },
    "task_class_keywords": {
        "investigation": ["investigate", "evidence", "incident", "threat", "attack", "ioc"],
        "extraction": ["extract", "parse", "schema", "json", "structure", "entity"],
        "synthesis": ["write", "summarize", "report", "narrative", "document"],
        "reasoning": ["reason", "logic", "math", "complex", "proof", "algorithm"],
        "safety": ["safe", "risk", "policy", "harm", "guardrail", "compliance"],
        "code_review": ["code", "bug", "function", "error", "security", "vulnerability"],
    }
}
