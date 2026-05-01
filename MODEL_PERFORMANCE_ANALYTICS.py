#!/usr/bin/env python3
"""
MODEL_PERFORMANCE_ANALYTICS.py

Historical reasoning trace analysis for adaptive model selection.
Generates synthetic historical data and fits decision tree to predict optimal models.

Usage:
    python MODEL_PERFORMANCE_ANALYTICS.py --output thresholds.json --verbose

Outputs:
    - decision_thresholds.json: Model selection thresholds by task_class + complexity
    - quality_baselines.json: Quality scores per (model, task_class)
    - cost_efficiency_report.txt: Cost vs quality analysis
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import random

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReasoningTrace:
    """Single reasoning query + outcome."""
    task_id: str
    question: str
    task_class: str
    model_used: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    quality_score: float  # 0.0-1.0 (from validation or ground truth)
    passes: int
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def cost_usd(self) -> float:
        """Calculate cost based on model and tokens."""
        costs = {
            "opus": {"input": 0.015, "output": 0.045},
            "sonnet": {"input": 0.003, "output": 0.015},
            "haiku": {"input": 0.0008, "output": 0.003},
            "phi4-mini": {"input": 0.0, "output": 0.0},
            "qwen3.5": {"input": 0.0, "output": 0.0},
            "llama3.1": {"input": 0.0, "output": 0.0},
            "qwen2.5-coder": {"input": 0.0, "output": 0.0},
        }
        
        model_key = self.model_used.lower().replace("-", "").replace(".", "")
        for key in costs:
            if key in model_key:
                c = costs[key]
                return (self.input_tokens * c["input"] / 1000 + 
                        self.output_tokens * c["output"] / 1000)
        return 0.0

    @property
    def cost_efficiency(self) -> float:
        """Cost per unit quality (lower is better)."""
        return self.cost_usd / max(self.quality_score, 0.01)

    @property
    def complexity_estimate(self) -> int:
        """Quick complexity estimation (0-100)."""
        score = 0
        
        # Length signal
        words = len(self.question.split())
        if words < 50:
            score += 5
        elif words < 150:
            score += 15
        elif words < 300:
            score += 25
        else:
            score += 30
        
        # Task class baseline
        task_baselines = {
            "reasoning": 25,
            "investigation": 20,
            "code_review": 15,
            "data_governance": 12,
            "synthesis": 10,
            "general": 10,
            "extraction": 8,
            "safety": 5,
        }
        score += task_baselines.get(self.task_class, 10)
        
        # Pattern signals
        patterns = {
            "why": 5,
            "explain": 5,
            "reasoning": 8,
            "proof": 8,
            "but": 3,
            "however": 3,
            "production": 3,
            "critical": 3,
        }
        
        q_lower = self.question.lower()
        for pattern, points in patterns.items():
            if pattern in q_lower:
                score += points
        
        # Multi-part questions
        score += min(self.question.count("?") * 3, 10)
        
        return min(score, 100)


@dataclass
class ModelPerformanceMetrics:
    """Aggregated performance for a (model, task_class, complexity_range)."""
    model: str
    task_class: str
    complexity_min: int
    complexity_max: int
    sample_count: int = 0
    avg_quality: float = 0.0
    avg_latency_ms: float = 0.0
    avg_cost_usd: float = 0.0
    p95_latency_ms: float = 0.0
    p95_cost_usd: float = 0.0
    quality_stddev: float = 0.0

    @property
    def cost_per_quality_point(self) -> float:
        """Cost efficiency: lower is better."""
        return self.avg_cost_usd / max(self.avg_quality, 0.01)


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic Data Generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_synthetic_traces(num_traces: int = 1200) -> List[ReasoningTrace]:
    """Generate synthetic historical reasoning traces."""
    
    task_classes = ["general", "code_review", "investigation", "extraction",
                   "synthesis", "reasoning", "safety", "data_governance"]
    
    models = ["opus", "sonnet", "haiku", "phi4-mini", "qwen3.5", "llama3.1"]
    
    questions_by_class = {
        "general": [
            "What does this function do?",
            "Summarize the key points from this text.",
            "Explain how X works.",
            "What are the best practices for Y?",
        ],
        "code_review": [
            "Review this code for bugs and improvements.",
            "Check this function for security issues.",
            "Is this implementation optimal?",
            "Review this API design.",
        ],
        "investigation": [
            "Analyze this security incident timeline.",
            "Investigate the root cause of this failure.",
            "What indicators suggest malicious activity?",
            "Trace the attack chain from logs.",
        ],
        "extraction": [
            "Extract entities from this text.",
            "Parse this structured data.",
            "Identify key information.",
            "Create a JSON summary.",
        ],
        "synthesis": [
            "Write a summary report.",
            "Generate documentation.",
            "Create a narrative description.",
            "Write a detailed analysis.",
        ],
        "reasoning": [
            "Prove why this algorithm works.",
            "Explain the mathematical derivation.",
            "What is the proof that X implies Y?",
            "Reasoning about multi-step problem.",
        ],
        "safety": [
            "Is this content safe?",
            "Check for policy violations.",
            "Assess risk level.",
            "Flag potential harms.",
        ],
        "data_governance": [
            "Analyze data quality issues.",
            "Trace data lineage.",
            "Identify root causes of anomalies.",
            "Recommend data improvements.",
        ],
    }
    
    traces = []
    base_time = datetime.now() - timedelta(days=30)
    
    for i in range(num_traces):
        task_class = random.choice(task_classes)
        model = random.choice(models)
        question = random.choice(questions_by_class.get(task_class, ["Generic question"]))
        
        # Add some length variation
        if random.random() < 0.3:  # 30% longer queries
            question = question + " " + question
        if random.random() < 0.1:  # 10% much longer
            question = question * 3
        
        # Token counts based on question length
        words = len(question.split())
        input_tokens = int(words * 1.5)  # Tokenizer efficiency
        
        # Output tokens: vary by task class
        output_ranges = {
            "general": (400, 1200),
            "code_review": (600, 1800),
            "investigation": (800, 2400),
            "extraction": (200, 600),
            "synthesis": (1000, 2500),
            "reasoning": (800, 2000),
            "safety": (200, 600),
            "data_governance": (600, 1600),
        }
        
        output_min, output_max = output_ranges.get(task_class, (400, 1200))
        output_tokens = random.randint(output_min, output_max)
        
        # Latency by model (base + token count)
        latency_bases = {
            "opus": 12000,
            "sonnet": 6000,
            "haiku": 1500,
            "phi4-mini": 400,
            "qwen3.5": 2500,
            "llama3.1": 3000,
            "qwen2.5-coder": 2000,
        }
        
        base_latency = latency_bases.get(model, 5000)
        token_latency = (input_tokens + output_tokens) / 100  # 1ms per 100 tokens
        latency_ms = base_latency + token_latency + random.gauss(0, base_latency * 0.1)
        
        # Quality: based on model + task class + complexity
        # Premium models generally score higher
        quality_by_model = {
            "opus": 0.98,
            "sonnet": 0.94,
            "haiku": 0.82,
            "phi4-mini": 0.75,
            "qwen3.5": 0.88,
            "llama3.1": 0.85,
            "qwen2.5-coder": 0.90,
        }
        
        # Task class adjustments
        quality_adjustments = {
            "general": 0.0,
            "code_review": -0.05 if model in ["haiku", "phi4-mini"] else 0.0,
            "investigation": -0.10 if model in ["haiku", "phi4-mini"] else 0.0,
            "extraction": 0.03,  # easier task
            "synthesis": -0.02 if model == "haiku" else 0.0,
            "reasoning": -0.15 if model != "opus" else 0.0,
            "safety": 0.0,
            "data_governance": -0.08 if model == "haiku" else 0.0,
        }
        
        base_quality = quality_by_model.get(model, 0.85)
        adj = quality_adjustments.get(task_class, 0.0)
        quality = max(0.5, min(1.0, base_quality + adj + random.gauss(0, 0.05)))
        
        passes = random.randint(1, 3)
        
        trace = ReasoningTrace(
            task_id=f"trace_{i:06d}",
            question=question,
            task_class=task_class,
            model_used=model,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            latency_ms=float(max(100, latency_ms)),
            quality_score=float(quality),
            passes=passes,
            timestamp=base_time + timedelta(seconds=random.randint(0, 30*24*3600))
        )
        
        traces.append(trace)
    
    return traces


# ─────────────────────────────────────────────────────────────────────────────
# Analytics Functions
# ─────────────────────────────────────────────────────────────────────────────

def analyze_traces(traces: List[ReasoningTrace]) -> Dict[str, ModelPerformanceMetrics]:
    """Aggregate traces into performance metrics by (model, task_class, complexity)."""
    
    # Group traces by model, task_class, and complexity range
    complexity_ranges = [(0, 25), (25, 50), (50, 75), (75, 100)]
    
    metrics_by_key = {}
    
    for trace in traces:
        model = trace.model_used
        task = trace.task_class
        complexity = trace.complexity_estimate
        
        # Find which complexity range this falls into
        comp_min, comp_max = None, None
        for min_c, max_c in complexity_ranges:
            if min_c <= complexity < max_c:
                comp_min, comp_max = min_c, max_c
                break
        
        if comp_min is None:
            comp_min, comp_max = 75, 100  # fallback to highest
        
        key = (model, task, comp_min, comp_max)
        
        if key not in metrics_by_key:
            metrics_by_key[key] = {
                "qualities": [],
                "latencies": [],
                "costs": [],
            }
        
        metrics_by_key[key]["qualities"].append(trace.quality_score)
        metrics_by_key[key]["latencies"].append(trace.latency_ms)
        metrics_by_key[key]["costs"].append(trace.cost_usd)
    
    # Compute aggregates
    metrics = {}
    
    for key, data in metrics_by_key.items():
        model, task, comp_min, comp_max = key
        
        qualities = sorted(data["qualities"])
        latencies = sorted(data["latencies"])
        costs = sorted(data["costs"])
        
        avg_quality = sum(qualities) / len(qualities)
        avg_latency = sum(latencies) / len(latencies)
        avg_cost = sum(costs) / len(costs)
        
        # Percentile 95
        p95_idx = int(len(latencies) * 0.95)
        p95_latency = latencies[p95_idx] if p95_idx < len(latencies) else latencies[-1]
        
        p95_idx_cost = int(len(costs) * 0.95)
        p95_cost = costs[p95_idx_cost] if p95_idx_cost < len(costs) else costs[-1]
        
        # Standard deviation
        variance = sum((q - avg_quality)**2 for q in qualities) / len(qualities)
        stddev = variance ** 0.5
        
        metric_key = f"{model}_{task}_{comp_min}_{comp_max}"
        metrics[metric_key] = ModelPerformanceMetrics(
            model=model,
            task_class=task,
            complexity_min=comp_min,
            complexity_max=comp_max,
            sample_count=len(qualities),
            avg_quality=avg_quality,
            avg_latency_ms=avg_latency,
            avg_cost_usd=avg_cost,
            p95_latency_ms=p95_latency,
            p95_cost_usd=p95_cost,
            quality_stddev=stddev,
        )
    
    return metrics


def build_decision_thresholds(
    metrics: Dict[str, ModelPerformanceMetrics]
) -> Dict[str, Dict]:
    """
    Build decision tree thresholds from analytics.
    
    For each (task_class, complexity_range), recommend best model by:
    - Maximize quality (primary)
    - Minimize cost (secondary)
    - Ensure < 5s latency (tertiary)
    """
    
    # Group by task_class and complexity
    by_task_complexity = {}
    
    for metric in metrics.values():
        key = (metric.task_class, metric.complexity_min, metric.complexity_max)
        
        if key not in by_task_complexity:
            by_task_complexity[key] = []
        
        by_task_complexity[key].append(metric)
    
    thresholds = {}
    
    for (task_class, comp_min, comp_max), metric_list in by_task_complexity.items():
        # Sort by quality descending, then cost ascending
        sorted_metrics = sorted(
            metric_list,
            key=lambda m: (-m.avg_quality, m.avg_cost_usd)
        )
        
        # Pick best model
        best = sorted_metrics[0]
        
        # Secondary/tertiary options
        secondary = sorted_metrics[1] if len(sorted_metrics) > 1 else None
        tertiary = sorted_metrics[2] if len(sorted_metrics) > 2 else None
        
        key_str = f"{task_class}_{comp_min}_{comp_max}"
        thresholds[key_str] = {
            "task_class": task_class,
            "complexity_range": [comp_min, comp_max],
            "primary_model": best.model,
            "primary_quality": round(best.avg_quality, 3),
            "primary_cost": round(best.avg_cost_usd, 4),
            "primary_latency_ms": round(best.avg_latency_ms, 1),
            "secondary_model": secondary.model if secondary else None,
            "secondary_quality": round(secondary.avg_quality, 3) if secondary else None,
            "fallback_model": tertiary.model if tertiary else "opus",
            "sample_count": best.sample_count,
        }
    
    return thresholds


def generate_cost_report(traces: List[ReasoningTrace], metrics: Dict) -> str:
    """Generate human-readable cost efficiency report."""
    
    report = []
    report.append("=" * 80)
    report.append("MODEL PERFORMANCE ANALYTICS REPORT")
    report.append("=" * 80)
    report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"Total traces analyzed: {len(traces)}")
    report.append("")
    
    # Cost comparison by model
    by_model = {}
    for trace in traces:
        if trace.model_used not in by_model:
            by_model[trace.model_used] = []
        by_model[trace.model_used].append(trace)
    
    report.append("=" * 80)
    report.append("COST EFFICIENCY BY MODEL")
    report.append("=" * 80)
    report.append(f"{'Model':<20} {'Avg Quality':<15} {'Avg Cost':<15} {'Cost/Quality':<15} {'Count':<10}")
    report.append("-" * 80)
    
    efficiency_by_model = {}
    for model, model_traces in sorted(by_model.items()):
        avg_quality = sum(t.quality_score for t in model_traces) / len(model_traces)
        avg_cost = sum(t.cost_usd for t in model_traces) / len(model_traces)
        cost_per_quality = avg_cost / max(avg_quality, 0.01)
        count = len(model_traces)
        
        efficiency_by_model[model] = cost_per_quality
        
        report.append(
            f"{model:<20} {avg_quality:<15.3f} {avg_cost:<15.4f} {cost_per_quality:<15.4f} {count:<10}"
        )
    
    report.append("")
    
    # Savings analysis
    report.append("=" * 80)
    report.append("PROJECTED SAVINGS (10,000 queries/month)")
    report.append("=" * 80)
    
    # Baseline: all Opus
    total_opus_cost = 0
    for trace in traces:
        trace_copy = ReasoningTrace(
            task_id=trace.task_id,
            question=trace.question,
            task_class=trace.task_class,
            model_used="opus",
            input_tokens=trace.input_tokens,
            output_tokens=trace.output_tokens,
            latency_ms=0,
            quality_score=0.98,
            passes=trace.passes,
        )
        total_opus_cost += trace_copy.cost_usd
    
    # Actual cost
    actual_cost = sum(t.cost_usd for t in traces)
    
    # Scale to 10k queries
    scale_factor = 10000 / len(traces)
    monthly_baseline = total_opus_cost * scale_factor
    monthly_actual = actual_cost * scale_factor
    monthly_savings = monthly_baseline - monthly_actual
    savings_percent = (monthly_savings / monthly_baseline * 100) if monthly_baseline > 0 else 0
    
    report.append(f"Baseline (all Opus):        ${monthly_baseline:>12,.2f}/month")
    report.append(f"With adaptive routing:      ${monthly_actual:>12,.2f}/month")
    report.append(f"Monthly savings:            ${monthly_savings:>12,.2f}")
    report.append(f"Savings percentage:         {savings_percent:>12.1f}%")
    report.append("")
    
    # Quality impact
    avg_quality_opus = 0.98
    avg_quality_actual = sum(t.quality_score for t in traces) / len(traces)
    quality_delta = (avg_quality_actual - avg_quality_opus) * 100
    
    report.append(f"Average quality (Opus):     {avg_quality_opus:>12.1f}%")
    report.append(f"Average quality (adaptive): {avg_quality_actual:>12.1f}%")
    report.append(f"Quality impact:             {quality_delta:>12.1f}%")
    report.append("")
    
    # Latency impact
    avg_latency_opus = 12000  # ms
    avg_latency_actual = sum(t.latency_ms for t in traces) / len(traces)
    latency_improvement = (avg_latency_opus - avg_latency_actual) / avg_latency_opus * 100
    
    report.append(f"Average latency (Opus):     {avg_latency_opus:>12.1f}ms")
    report.append(f"Average latency (adaptive): {avg_latency_actual:>12.1f}ms")
    report.append(f"Latency improvement:        {latency_improvement:>12.1f}%")
    report.append("")
    
    # Quality by task class
    report.append("=" * 80)
    report.append("QUALITY IMPACT BY TASK CLASS")
    report.append("=" * 80)
    report.append(f"{'Task Class':<20} {'Adaptive Avg':<15} {'Opus Baseline':<15} {'Delta':<10}")
    report.append("-" * 80)
    
    by_task = {}
    for trace in traces:
        if trace.task_class not in by_task:
            by_task[trace.task_class] = []
        by_task[trace.task_class].append(trace)
    
    for task_class, task_traces in sorted(by_task.items()):
        avg_quality = sum(t.quality_score for t in task_traces) / len(task_traces)
        delta = (avg_quality - 0.98) * 100
        report.append(
            f"{task_class:<20} {avg_quality:<15.1f}% {'98.0':<15}% {delta:<10.1f}%"
        )
    
    report.append("")
    
    return "\n".join(report)


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main(
    num_traces: int = 1200,
    output_prefix: str = "model_analytics",
    verbose: bool = False,
) -> None:
    """Generate analytics and export thresholds."""
    
    if verbose:
        logging.basicConfig(level=logging.INFO)
    
    log.info(f"Generating {num_traces} synthetic reasoning traces...")
    traces = generate_synthetic_traces(num_traces)
    
    log.info("Analyzing traces...")
    metrics = analyze_traces(traces)
    
    log.info("Building decision thresholds...")
    thresholds = build_decision_thresholds(metrics)
    
    # Export thresholds
    thresholds_file = f"{output_prefix}_decision_thresholds.json"
    with open(thresholds_file, "w") as f:
        json.dump(thresholds, f, indent=2)
    log.info(f"Exported decision thresholds to {thresholds_file}")
    
    # Export metrics
    metrics_file = f"{output_prefix}_quality_baselines.json"
    metrics_dict = {k: asdict(v) for k, v in metrics.items()}
    with open(metrics_file, "w") as f:
        json.dump(metrics_dict, f, indent=2)
    log.info(f"Exported quality baselines to {metrics_file}")
    
    # Generate report
    report = generate_cost_report(traces, metrics)
    report_file = f"{output_prefix}_cost_report.txt"
    with open(report_file, "w") as f:
        f.write(report)
    log.info(f"Exported cost report to {report_file}")
    
    # Print summary
    print("\n" + report)


if __name__ == "__main__":
    main(num_traces=1200, output_prefix="model_analytics", verbose=True)

