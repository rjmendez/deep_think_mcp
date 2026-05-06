"""Comprehensive tests for the Nova fact-checking / verification pipeline.

Covers:
  1.  ClaimExtractor — basic extraction
  2.  ClaimExtractor — skips questions and imperatives
  3.  ClaimExtractor — skips meta-commentary
  4.  ClaimExtractor — inline confidence parsing
  5.  ClaimExtractor — deduplication
  6.  ClaimExtractor — claim type classification (causal, numerical, categorical, assertion)
  7.  NovaVerificationClient — verdict mapping (SUPPORTED → TRUE)
  8.  NovaVerificationClient — verdict mapping (CONTRADICTED → FALSE)
  9.  NovaVerificationClient — verdict mapping (INSUFFICIENT_EVIDENCE → UNCERTAIN)
  10. NovaVerificationClient — network error → ERROR status, no exception raised
  11. NovaVerificationClient — 401 auth error → ERROR status
  12. NovaVerificationClient — retry logic on transient failure
  13. NovaVerificationClient — verify_batch concurrency + ordering
  14. ConfidenceRecalculator — TRUE claims boost confidence
  15. ConfidenceRecalculator — FALSE claims penalise confidence
  16. ConfidenceRecalculator — UNCERTAIN claims adjust minimally
  17. ConfidenceRecalculator — mixed claims combine correctly
  18. ConfidenceRecalculator — clamping to [0, 1]
  19. ConfidenceRecalculator — empty results → unchanged confidence
  20. VerificationPipeline — happy-path enriches result dict
  21. VerificationPipeline — adds verification_results, adjusted_final_confidence, verification_summary
  22. VerificationPipeline — no claims → verification_results = []
  23. VerificationPipeline — low-confidence UNCERTAIN claims escalated
  24. VerificationPipeline — ERROR claims escalated
  25. VerificationPipeline — Nova failure is non-fatal (pipeline does not raise)
  26. HumanEscalationQueue — drain returns all items and clears queue
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import target modules
# ---------------------------------------------------------------------------

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nova_factcheck.extractor import ClaimExtractor, ExtractedClaim
from nova_factcheck.nova_client import (
    ClaimVerificationResult,
    NovaVerificationClient,
    VerificationStatus,
    _map_verdict,
)
from nova_factcheck.recalculator import (
    BOOST_TRUE,
    PENALTY_FALSE,
    PENALTY_UNCERTAIN,
    ConfidenceRecalculator,
)
from nova_factcheck.escalation import (
    EscalationItem,
    HumanEscalationQueue,
    LOW_CONFIDENCE_THRESHOLD,
)
from nova_factcheck.pipeline import VerificationPipeline


# ===========================================================================
# 1. ClaimExtractor — basic extraction
# ===========================================================================

def test_extractor_basic_assertion():
    extractor = ClaimExtractor()
    claims = extractor.extract("Python is a high-level programming language used worldwide.")
    assert len(claims) >= 1
    assert any("Python" in c.text for c in claims)


# ===========================================================================
# 2. ClaimExtractor — skips questions and imperatives
# ===========================================================================

def test_extractor_skips_questions():
    extractor = ClaimExtractor()
    claims = extractor.extract("What is the capital of France? Is this correct?")
    assert len(claims) == 0


def test_extractor_skips_imperatives():
    extractor = ClaimExtractor()
    claims = extractor.extract("Let us consider the evidence. Note that this is important. Use caution.")
    assert len(claims) == 0


# ===========================================================================
# 3. ClaimExtractor — skips meta-commentary
# ===========================================================================

def test_extractor_skips_meta_commentary():
    extractor = ClaimExtractor()
    claims = extractor.extract("In conclusion, this summarizes the findings. For example, see the list above.")
    assert len(claims) == 0


# ===========================================================================
# 4. ClaimExtractor — inline confidence parsing
# ===========================================================================

def test_extractor_inline_confidence():
    extractor = ClaimExtractor()
    claims = extractor.extract("The model has 92% accuracy [confidence: 90%].")
    assert len(claims) >= 1
    assert claims[0].confidence_in_text == pytest.approx(0.9, abs=0.01)
    # Confidence marker should be stripped from text
    assert "confidence" not in claims[0].text.lower() or "90%" not in claims[0].text


def test_extractor_default_confidence_when_absent():
    extractor = ClaimExtractor()
    claims = extractor.extract("The database has 1000 records stored.")
    assert len(claims) >= 1
    assert claims[0].confidence_in_text == pytest.approx(0.5, abs=0.01)


# ===========================================================================
# 5. ClaimExtractor — deduplication
# ===========================================================================

def test_extractor_deduplication():
    extractor = ClaimExtractor()
    text = (
        "The sky is blue and has been observed across cultures.\n"
        "The sky is blue and has been observed across cultures.\n"
        "The sky is blue and has been observed across cultures."
    )
    claims = extractor.extract(text)
    texts = [c.text for c in claims]
    assert len(texts) == len(set(t.lower().strip() for t in texts)), "Duplicate claims found"


# ===========================================================================
# 6. ClaimExtractor — claim type classification
# ===========================================================================

def test_extractor_causal_type():
    extractor = ClaimExtractor()
    claims = extractor.extract("Smoking causes lung cancer in exposed populations.")
    assert any(c.claim_type == "causal" for c in claims)


def test_extractor_numerical_type():
    extractor = ClaimExtractor()
    claims = extractor.extract("The latency measures 12ms under normal load conditions.")
    assert any(c.claim_type == "numerical" for c in claims)


def test_extractor_categorical_type():
    extractor = ClaimExtractor()
    claims = extractor.extract("Python is a type of interpreted programming language.")
    # Should be categorical (contains "is a type of")
    assert any(c.claim_type in ("categorical", "assertion") for c in claims)


def test_extractor_assertion_type():
    extractor = ClaimExtractor()
    claims = extractor.extract("The experiment confirms the expected results are consistent.")
    assert any(c.claim_type == "assertion" for c in claims)


# ===========================================================================
# 7-9. NovaVerificationClient — verdict mapping
# ===========================================================================

def test_verdict_supported_maps_to_true():
    assert _map_verdict("SUPPORTED") == VerificationStatus.TRUE


def test_verdict_contradicted_maps_to_false():
    assert _map_verdict("CONTRADICTED") == VerificationStatus.FALSE


def test_verdict_insufficient_evidence_maps_to_uncertain():
    assert _map_verdict("INSUFFICIENT_EVIDENCE") == VerificationStatus.UNCERTAIN


def test_verdict_unknown_maps_to_uncertain():
    assert _map_verdict("RANDOM_GARBAGE") == VerificationStatus.UNCERTAIN


# ===========================================================================
# 10. NovaVerificationClient — network error → ERROR, no exception
# ===========================================================================

@pytest.mark.asyncio
async def test_nova_client_network_error_returns_error_status():
    import aiohttp
    client = NovaVerificationClient(retries=0)

    with patch.object(client, "_post_verify", side_effect=aiohttp.ClientConnectionError("refused")):
        result = await client.verify("The sky is blue.", claim_id="c1")

    assert result.status == VerificationStatus.ERROR
    assert result.nova_confidence == 0.0
    assert result.claim_id == "c1"


# ===========================================================================
# 11. NovaVerificationClient — 401 auth error → ERROR
# ===========================================================================

@pytest.mark.asyncio
async def test_nova_client_auth_error_returns_error_status():
    import aiohttp
    client = NovaVerificationClient(retries=0, token="token", totp_seed="seed")

    mock_resp_info = MagicMock()
    auth_error = aiohttp.ClientResponseError(
        mock_resp_info, (), status=401, message="Unauthorized"
    )

    with patch.object(client, "_post_verify", side_effect=auth_error):
        result = await client.verify("Test claim.", claim_id="c2")

    assert result.status == VerificationStatus.ERROR
    assert result.error_kind == "auth_failed"


@pytest.mark.asyncio
async def test_nova_client_missing_auth_short_circuits(monkeypatch):
    monkeypatch.delenv("NOVA_TOKEN", raising=False)
    monkeypatch.delenv("NOVA_TOTP_SEED", raising=False)

    client = NovaVerificationClient(retries=0)
    result = await client.verify("Test claim.", claim_id="c-missing-auth")

    assert result.status == VerificationStatus.ERROR
    assert result.error_kind == "auth_config_missing"
    assert "missing" in result.reasoning.lower()


# ===========================================================================
# 12. NovaVerificationClient — retry logic
# ===========================================================================

@pytest.mark.asyncio
async def test_nova_client_retries_on_transient_failure():
    import aiohttp
    client = NovaVerificationClient(retries=2, token="token", totp_seed="seed")

    call_count = 0

    async def flaky(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise aiohttp.ClientConnectionError("transient")
        # Third attempt succeeds
        return {"verdict": "SUPPORTED", "confidence": 0.9, "reasoning": "ok", "evidence": []}

    with patch.object(client, "_post_verify", side_effect=flaky):
        with patch("asyncio.sleep", new_callable=AsyncMock):  # speed up
            result = await client.verify("Test.", claim_id="c3")

    assert call_count == 3
    assert result.status == VerificationStatus.TRUE
    assert result.nova_confidence == pytest.approx(0.9)


# ===========================================================================
# 13. NovaVerificationClient — verify_batch ordering
# ===========================================================================

@pytest.mark.asyncio
async def test_nova_client_verify_batch_preserves_order():
    client = NovaVerificationClient()
    claims = [("id1", "Claim one."), ("id2", "Claim two."), ("id3", "Claim three.")]

    async def mock_verify(text, claim_id=""):
        return ClaimVerificationResult(
            claim_id=claim_id,
            claim_text=text,
            status=VerificationStatus.TRUE,
            nova_confidence=0.8,
            reasoning="mock",
            evidence=[],
            latency_ms=5,
        )

    with patch.object(client, "verify", side_effect=mock_verify):
        results = await client.verify_batch(claims)

    assert [r.claim_id for r in results] == ["id1", "id2", "id3"]


# ===========================================================================
# 14-18. ConfidenceRecalculator
# ===========================================================================

def _make_vr(status: VerificationStatus, claim_id: str = "x") -> ClaimVerificationResult:
    return ClaimVerificationResult(
        claim_id=claim_id,
        claim_text="test",
        status=status,
        nova_confidence=0.7,
        reasoning="",
        evidence=[],
        latency_ms=10,
    )


def test_recalculator_true_boosts():
    calc = ConfidenceRecalculator()
    result = calc.recalculate(0.5, [_make_vr(VerificationStatus.TRUE)])
    assert result.adjusted_final_confidence == pytest.approx(0.5 + BOOST_TRUE, abs=1e-4)


def test_recalculator_false_penalises():
    calc = ConfidenceRecalculator()
    result = calc.recalculate(0.5, [_make_vr(VerificationStatus.FALSE)])
    assert result.adjusted_final_confidence == pytest.approx(0.5 + PENALTY_FALSE, abs=1e-4)


def test_recalculator_uncertain_minimal():
    calc = ConfidenceRecalculator()
    result = calc.recalculate(0.5, [_make_vr(VerificationStatus.UNCERTAIN)])
    assert result.adjusted_final_confidence == pytest.approx(0.5 + PENALTY_UNCERTAIN, abs=1e-4)


def test_recalculator_mixed():
    calc = ConfidenceRecalculator()
    # 2 TRUE, 1 FALSE → mean_delta = (0.1 + 0.1 - 0.3) / 3 = -0.1/3 ≈ -0.0333
    vrs = [
        _make_vr(VerificationStatus.TRUE, "t1"),
        _make_vr(VerificationStatus.TRUE, "t2"),
        _make_vr(VerificationStatus.FALSE, "f1"),
    ]
    result = calc.recalculate(0.6, vrs)
    expected = 0.6 + (0.1 + 0.1 - 0.3) / 3
    assert result.adjusted_final_confidence == pytest.approx(expected, abs=1e-3)
    assert result.true_count == 2
    assert result.false_count == 1


def test_recalculator_clamps_to_bounds():
    calc = ConfidenceRecalculator()
    # Many FALSE claims — would push below 0
    vrs = [_make_vr(VerificationStatus.FALSE, f"f{i}") for i in range(10)]
    result = calc.recalculate(0.1, vrs)
    assert result.adjusted_final_confidence >= 0.0

    # Many TRUE claims — would push above 1
    vrs2 = [_make_vr(VerificationStatus.TRUE, f"t{i}") for i in range(10)]
    result2 = calc.recalculate(0.95, vrs2)
    assert result2.adjusted_final_confidence <= 1.0


def test_recalculator_empty_unchanged():
    calc = ConfidenceRecalculator()
    result = calc.recalculate(0.72, [])
    assert result.adjusted_final_confidence == pytest.approx(0.72)
    assert result.total_claims == 0


# ===========================================================================
# 20-21. VerificationPipeline — happy-path enrichment
# ===========================================================================

@pytest.mark.asyncio
async def test_pipeline_enriches_result():
    """Pipeline adds expected keys to the result dict."""
    deep_think_result = {
        "final_answer": "Python is an interpreted language. It was released in 1991.",
        "pass_outputs": ["Python is dynamically typed and runs on multiple platforms."],
        "confidence": 0.7,
    }

    fake_vr = ClaimVerificationResult(
        claim_id="x",
        claim_text="Python is an interpreted language.",
        status=VerificationStatus.TRUE,
        nova_confidence=0.9,
        reasoning="Supported by literature.",
        evidence=[],
        latency_ms=10,
    )

    mock_client = AsyncMock(spec=NovaVerificationClient)
    mock_client.verify_batch = AsyncMock(return_value=[fake_vr])

    pipeline = VerificationPipeline(nova_client=mock_client)
    result = await pipeline.run(deep_think_result, job_id="test-job")

    assert "verification_results" in result
    assert "adjusted_final_confidence" in result
    assert "verification_summary" in result
    assert isinstance(result["verification_results"], list)
    assert isinstance(result["adjusted_final_confidence"], float)


# ===========================================================================
# 22. VerificationPipeline — no claims → verification_results = []
# ===========================================================================

@pytest.mark.asyncio
async def test_pipeline_no_claims_empty_results():
    deep_think_result = {
        "final_answer": "?",
        "pass_outputs": [],
        "confidence": 0.5,
    }
    pipeline = VerificationPipeline()
    result = await pipeline.run(deep_think_result, job_id="empty-job")
    assert result["verification_results"] == []


def test_pipeline_collect_text_ignores_failed_pass_results():
    pipeline = VerificationPipeline(enabled=False)
    collected = pipeline._collect_text(
        {
            "final_answer": "Final clean answer.",
            "pass_results": [
                {"status": "failed", "output": "", "error": "Timeout calling model"},
                {"status": "complete", "output": "Clean pass output."},
            ],
            "pass_outputs": ["[ERROR: leaked legacy text]"],
        }
    )

    assert "Final clean answer." in collected
    assert "Clean pass output." in collected
    assert "Timeout calling model" not in collected
    assert "[ERROR:" not in collected


# ===========================================================================
# 23. VerificationPipeline — low-confidence UNCERTAIN claims escalated
# ===========================================================================

@pytest.mark.asyncio
async def test_pipeline_escalates_low_confidence_uncertain():
    deep_think_result = {
        "final_answer": "The system has 100 active users and was deployed in 2019.",
        "pass_outputs": [],
        "confidence": 0.5,
    }

    def _uncertain_vr(claim_id, claim_text):
        return ClaimVerificationResult(
            claim_id=claim_id,
            claim_text=claim_text,
            status=VerificationStatus.UNCERTAIN,
            nova_confidence=0.3,
            reasoning="Not enough evidence.",
            evidence=[],
            latency_ms=5,
        )

    mock_client = AsyncMock(spec=NovaVerificationClient)
    mock_client.verify_batch = AsyncMock(
        side_effect=lambda pairs, **_kw: [_uncertain_vr(cid, ct) for cid, ct in pairs]
    )

    queue = HumanEscalationQueue()
    # Patch extractor to produce a claim with confidence below threshold
    from nova_factcheck.extractor import ExtractedClaim
    low_conf_claim = ExtractedClaim(
        claim_id="esc1",
        text="The system has 100 active users.",
        claim_type="numerical",
        confidence_in_text=LOW_CONFIDENCE_THRESHOLD - 0.1,  # below threshold
    )

    with patch.object(
        VerificationPipeline, "_collect_text", return_value=low_conf_claim.text
    ), patch(
        "nova_factcheck.pipeline.ClaimExtractor.extract", return_value=[low_conf_claim]
    ):
        pipeline = VerificationPipeline(nova_client=mock_client, escalation_queue=queue)
        await pipeline.run(deep_think_result, job_id="esc-job")

    items = queue.drain()
    assert any(i.claim_id == "esc1" for i in items)


# ===========================================================================
# 24. VerificationPipeline — ERROR claims escalated
# ===========================================================================

@pytest.mark.asyncio
async def test_pipeline_escalates_error_claims():
    deep_think_result = {
        "final_answer": "Water boils at 100 degrees Celsius at sea level.",
        "pass_outputs": [],
        "confidence": 0.6,
    }

    def _error_vr(claim_id, claim_text):
        return ClaimVerificationResult(
            claim_id=claim_id,
            claim_text=claim_text,
            status=VerificationStatus.ERROR,
            nova_confidence=0.0,
            reasoning="Network error",
            evidence=[],
            latency_ms=0,
        )

    mock_client = AsyncMock(spec=NovaVerificationClient)
    mock_client.verify_batch = AsyncMock(
        side_effect=lambda pairs, **_kw: [_error_vr(cid, ct) for cid, ct in pairs]
    )

    queue = HumanEscalationQueue()
    from nova_factcheck.extractor import ExtractedClaim
    error_claim = ExtractedClaim(
        claim_id="err1",
        text="Water boils at 100 degrees Celsius at sea level.",
        claim_type="assertion",
        confidence_in_text=0.8,
    )

    with patch.object(
        VerificationPipeline, "_collect_text", return_value=error_claim.text
    ), patch(
        "nova_factcheck.pipeline.ClaimExtractor.extract", return_value=[error_claim]
    ):
        pipeline = VerificationPipeline(nova_client=mock_client, escalation_queue=queue)
        result = await pipeline.run(deep_think_result, job_id="err-job")

    items = queue.drain()
    assert any(i.claim_id == "err1" for i in items)
    assert result.get("escalated_claim_ids")


# ===========================================================================
# 25. VerificationPipeline — Nova failure is non-fatal
# ===========================================================================

@pytest.mark.asyncio
async def test_pipeline_nova_failure_non_fatal():
    deep_think_result = {
        "final_answer": "The earth orbits the sun.",
        "pass_outputs": [],
        "confidence": 0.8,
    }

    mock_client = AsyncMock(spec=NovaVerificationClient)
    mock_client.verify_batch = AsyncMock(side_effect=Exception("Unexpected crash"))

    # Pipeline should not propagate exception
    pipeline = VerificationPipeline(nova_client=mock_client)
    # The pipeline will raise internally but _run_job catches it;
    # here we test that the pipeline itself handles it gracefully
    try:
        result = await pipeline.run(deep_think_result, job_id="crash-job")
        # If it doesn't raise, the result should still be returned
        assert "final_answer" in result
    except Exception as exc:
        pytest.fail(f"Pipeline raised unexpectedly: {exc}")


@pytest.mark.asyncio
async def test_pipeline_auth_failure_sets_verifier_status_without_penalty():
    deep_think_result = {
        "final_answer": "The earth orbits the sun.",
        "pass_outputs": [],
        "confidence": 0.8,
    }

    auth_error = ClaimVerificationResult(
        claim_id="auth1",
        claim_text="The earth orbits the sun.",
        status=VerificationStatus.ERROR,
        nova_confidence=0.0,
        reasoning="Nova authentication failed",
        evidence=[],
        latency_ms=3,
        error_kind="auth_failed",
    )

    mock_client = AsyncMock(spec=NovaVerificationClient)
    mock_client.verify_batch = AsyncMock(return_value=[auth_error])

    with patch(
        "nova_factcheck.pipeline.ClaimExtractor.extract",
        return_value=[
            ExtractedClaim(
                claim_id="auth1",
                text="The earth orbits the sun.",
                claim_type="assertion",
                confidence_in_text=0.8,
            )
        ],
    ):
        pipeline = VerificationPipeline(nova_client=mock_client)
        result = await pipeline.run(deep_think_result, job_id="auth-job")

    assert result["verification_status"] == "auth_failed"
    assert result["verification_results"] == []
    assert result["adjusted_final_confidence"] == pytest.approx(0.8)
    assert result["verification_summary"]["status"] == "auth_failed"
    assert "escalated_claim_ids" not in result


# ===========================================================================
# 26. HumanEscalationQueue — drain returns all items and clears queue
# ===========================================================================

@pytest.mark.asyncio
async def test_escalation_queue_drain():
    queue = HumanEscalationQueue()

    for i in range(3):
        item = EscalationItem(
            claim_id=f"c{i}",
            claim_text=f"Claim {i}",
            claim_type="assertion",
            nova_status="UNCERTAIN",
            nova_confidence=0.3,
            confidence_in_text=0.2,
            reason="test",
            job_id="jtest",
        )
        await queue.put(item)

    assert queue.size() == 3
    drained = queue.drain()
    assert len(drained) == 3
    assert queue.size() == 0


# ===========================================================================
# Extra: pipeline disabled skips verification
# ===========================================================================

@pytest.mark.asyncio
async def test_pipeline_disabled_returns_unchanged():
    deep_think_result = {
        "final_answer": "Python is great.",
        "confidence": 0.7,
    }
    pipeline = VerificationPipeline(enabled=False)
    result = await pipeline.run(deep_think_result, job_id="disabled-job")
    assert "verification_results" not in result
    assert "adjusted_final_confidence" not in result
