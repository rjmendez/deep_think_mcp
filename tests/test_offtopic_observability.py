from engine import orchestrator
from deep_think_mcp import metrics as runtime_metrics


def test_off_topic_detector_flags_rewritten_question():
    off_topic, reason = orchestrator._is_off_topic_response(
        "Review deep_think_mcp grounding integrity in engine/orchestrator.py",
        "Question: Explain the difference between a neural network and a decision tree.\nAnswer: ...",
    )
    assert off_topic is True
    assert "different question context" in reason


def test_off_topic_detector_flags_long_low_overlap_answer():
    off_topic, reason = orchestrator._is_off_topic_response(
        "Audit retry behavior in orchestrator run_fan_out and cache gating",
        "Neural image style transfer uses convolutional layers and gradient descent to produce art-like results from photos.",
    )
    assert off_topic is True
    assert "overlap" in reason


def test_off_topic_detector_allows_short_valid_answer():
    off_topic, reason = orchestrator._is_off_topic_response(
        "Add retries and logging for off-topic response handling",
        "Added bounded retries, logs, and metrics.",
    )
    assert off_topic is False
    assert reason == ""


def test_cached_answer_quality_gate_allows_short_answer():
    ok, reason = orchestrator._passes_cached_answer_quality_gate(
        "Fix fan out retry handling",
        "Retries and logging were added.",
    )
    assert ok is True
    assert reason == ""


def test_confidence_normalization_parses_percent_string():
    normalized, warnings = orchestrator._normalize_synthesis_structured(
        {"confidence_score": "85.7%", "final_answer": "ok"}
    )
    assert normalized["confidence_score"] == 85
    assert warnings == []


def test_confidence_normalization_rejects_non_finite():
    normalized, warnings = orchestrator._normalize_synthesis_structured(
        {"confidence_score": "NaN", "final_answer": "ok"}
    )
    assert normalized["confidence_score"] == 0
    assert any("missing or invalid" in w for w in warnings)


def test_off_topic_metrics_recorded():
    runtime_metrics.reset_metrics()
    orchestrator._record_off_topic_outcome("detected")
    orchestrator._record_off_topic_outcome("retry_scheduled")
    metrics = runtime_metrics.get_metrics()
    assert metrics.off_topic_outcomes["detected"] == 1
    assert metrics.off_topic_outcomes["retry_scheduled"] == 1
