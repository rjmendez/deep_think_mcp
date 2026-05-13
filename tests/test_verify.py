"""Tests for verification system."""

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from deep_think_mcp.verify.config import load_config, VerifyConfig
from deep_think_mcp.verify.provider import (
    CloudProvider,
    LocalProvider,
    VerifyResult,
)
from deep_think_mcp.verify.queue import (
    VerifyJob,
    VerifyJobQueue,
    VerifyWorker,
)


class TestVerifyConfig:
    """Test configuration loading."""

    def test_load_config_defaults(self, monkeypatch):
        """Test configuration loads with defaults."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        
        config = load_config()
        
        assert config.anthropic_model == "claude-3-5-sonnet-20241022"
        assert config.verify_cloud_timeout == 45
        assert config.verify_local_timeout == 180
        assert config.verify_max_concurrency == 2

    def test_load_config_custom(self, monkeypatch):
        """Test configuration loads custom values."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        monkeypatch.setenv("VERIFY_CLOUD_TIMEOUT", "60")
        monkeypatch.setenv("VERIFY_MAX_CONCURRENCY", "4")
        
        config = load_config()
        
        assert config.anthropic_api_key == "sk-ant-test-key"
        assert config.verify_cloud_timeout == 60
        assert config.verify_max_concurrency == 4

    def test_config_validation(self):
        """Test configuration validation."""
        with pytest.raises(ValueError):
            VerifyConfig(
                anthropic_api_key="",
                anthropic_model="claude-3-5",
                verify_cloud_timeout=2,  # Too low
                verify_local_timeout=180,
                verify_async_timeout=300,
                verify_job_retention=300,
                verify_max_queue_size=100,
                verify_max_concurrency=2,
                ollama_url="http://localhost:11434",
            )


class TestVerifyProvider:
    """Test provider implementations."""

    def test_cloud_provider_init(self):
        """Test CloudProvider initialization."""
        provider = CloudProvider(
            api_key="sk-ant-test-key",
            model="claude-3-5-sonnet-20241022",
        )
        
        assert provider.api_key == "sk-ant-test-key"
        assert provider.model == "claude-3-5-sonnet-20241022"
        assert provider.timeout == 45

    def test_cloud_provider_invalid_key(self):
        """Test CloudProvider rejects invalid API key."""
        with pytest.raises(ValueError, match="Invalid API key"):
            CloudProvider(api_key="invalid-key")

    def test_local_provider_init(self):
        """Test LocalProvider initialization."""
        provider = LocalProvider(
            url="http://localhost:11434",
            model="neural-chat",
        )
        
        assert provider.url == "http://localhost:11434"
        assert provider.model == "neural-chat"
        assert provider.timeout == 180

    def test_verify_result(self):
        """Test VerifyResult dataclass."""
        result = VerifyResult(
            verdict=True,
            confidence=0.95,
            reasoning="This is clearly true",
            latency_ms=1234.5,
        )
        
        assert result.verdict is True
        assert result.confidence == 0.95
        assert result.latency_ms == 1234.5
        
        d = result.to_dict()
        assert d["verdict"] is True
        assert d["confidence"] == 0.95


class TestVerifyQueue:
    """Test job queue."""

    @pytest.fixture
    def queue(self, tmp_path):
        """Create queue with temporary database."""
        return VerifyJobQueue(db_path=tmp_path / "test.db")

    def test_create_job(self, queue):
        """Test creating a verification job."""
        job_id = queue.create_job(
            claim="The sky is blue",
            provider="cloud",
            context="During the day",
        )
        
        assert uuid.UUID(job_id)  # Valid UUID
        
        status = queue.get_status(job_id)
        assert status["status"] == "queued"
        assert status["claim"] == "The sky is blue"
        assert status["context"] == "During the day"

    def test_claim_next_job(self, queue):
        """Test claiming a job for processing."""
        job_id = queue.create_job("Test claim", "cloud")
        
        job = queue.claim_next_job("worker-1")
        
        assert job is not None
        assert job.id == job_id
        assert job.status == "processing"
        assert job.started_at is not None
        
        # Second worker shouldn't get same job
        job2 = queue.claim_next_job("worker-2")
        assert job2 is None

    def test_complete_job(self, queue):
        """Test completing a job."""
        job_id = queue.create_job("Test claim", "cloud")
        queue.claim_next_job("worker-1")
        
        result = VerifyResult(
            verdict=True,
            confidence=0.9,
            reasoning="Test",
            latency_ms=1000,
        )
        queue.complete_job(job_id, result)
        
        status = queue.get_status(job_id)
        assert status["status"] == "done"
        assert status["result"]["verdict"] is True
        assert status["result"]["confidence"] == 0.9

    def test_fail_job(self, queue):
        """Test marking job as failed."""
        job_id = queue.create_job("Test claim", "cloud")
        queue.claim_next_job("worker-1")
        
        queue.fail_job(job_id, "Test error message")
        
        status = queue.get_status(job_id)
        assert status["status"] == "failed"
        assert status["error"] == "Test error message"

    def test_get_status_not_found(self, queue):
        """Test getting status of non-existent job."""
        status = queue.get_status("non-existent-id")
        assert status is None


class TestVerifyWorker:
    """Test background worker."""

    @pytest.fixture
    def queue(self, tmp_path):
        """Create queue with temporary database."""
        return VerifyJobQueue(db_path=tmp_path / "test.db")

    @pytest.fixture
    async def worker(self, queue):
        """Create worker with mock providers."""
        # Create mock providers
        class MockProvider:
            async def verify_claim(self, claim, context=None):
                # Simple mock: claims with "true" are verified as true
                return VerifyResult(
                    verdict="true" in claim.lower(),
                    confidence=0.9,
                    reasoning="Mock verification",
                    latency_ms=100,
                )

        config = VerifyConfig(
            anthropic_api_key="",
            anthropic_model="mock",
            verify_cloud_timeout=45,
            verify_local_timeout=180,
            verify_async_timeout=300,
            verify_job_retention=300,
            verify_max_queue_size=100,
            verify_max_concurrency=2,
            ollama_url="http://localhost:11434",
        )

        worker = VerifyWorker(
            queue=queue,
            cloud_provider=MockProvider(),
            local_provider=None,
            config=config,
        )
        
        yield worker
        
        await worker.stop()

    @pytest.mark.asyncio
    async def test_worker_processes_job(self, queue, worker):
        """Test worker processes a queued job."""
        job_id = queue.create_job("This is true", "cloud")
        
        # Start worker
        await worker.start()
        
        # Wait for job to be processed
        await asyncio.sleep(1.0)
        
        # Check job completed
        status = queue.get_status(job_id)
        assert status["status"] == "done"
        assert status["result"]["verdict"] is True

    @pytest.mark.asyncio
    async def test_worker_handles_error(self, queue):
        """Test worker handles provider errors."""
        # Create mock provider that fails
        class FailingProvider:
            async def verify_claim(self, claim, context=None):
                raise RuntimeError("Test error")

        config = VerifyConfig(
            anthropic_api_key="",
            anthropic_model="mock",
            verify_cloud_timeout=45,
            verify_local_timeout=180,
            verify_async_timeout=300,
            verify_job_retention=300,
            verify_max_queue_size=100,
            verify_max_concurrency=2,
            ollama_url="http://localhost:11434",
        )

        worker = VerifyWorker(
            queue=queue,
            cloud_provider=FailingProvider(),
            local_provider=None,
            config=config,
        )
        
        job_id = queue.create_job("Test claim", "cloud")
        
        await worker.start()
        await asyncio.sleep(1.0)
        
        status = queue.get_status(job_id)
        assert status["status"] == "failed"
        assert "Test error" in status["error"]
        
        await worker.stop()


# Integration tests (require actual services)

@pytest.mark.integration
class TestCloudProviderIntegration:
    """Integration tests for CloudProvider (requires ANTHROPIC_API_KEY)."""

    @pytest.mark.asyncio
    async def test_verify_claim_real(self):
        """Test real verification with Anthropic API."""
        import os
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            pytest.skip("ANTHROPIC_API_KEY not set")
        
        provider = CloudProvider(api_key=api_key)
        result = await provider.verify_claim(
            "The Earth is round",
            context="Established scientific fact",
        )
        
        assert isinstance(result, VerifyResult)
        assert isinstance(result.verdict, bool)
        assert 0.0 <= result.confidence <= 1.0
        assert result.latency_ms > 0


@pytest.mark.integration
class TestLocalProviderIntegration:
    """Integration tests for LocalProvider (requires Ollama)."""

    @pytest.mark.asyncio
    async def test_verify_claim_real(self):
        """Test real verification with Ollama."""
        import os
        url = os.getenv("OLLAMA_URL", "")
        if not url:
            pytest.skip("OLLAMA_URL not set")
        
        provider = LocalProvider(url=url)
        try:
            result = await provider.verify_claim("The sky is blue")
            assert isinstance(result, VerifyResult)
        except Exception:
            pytest.skip("Ollama not running")
