"""Configuration for verification system."""

import os
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class VerifyConfig:
    """Verification system configuration."""

    anthropic_api_key: str
    anthropic_model: str
    verify_cloud_timeout: int
    verify_local_timeout: int
    verify_async_timeout: int
    verify_job_retention: int
    verify_max_queue_size: int
    verify_max_concurrency: int
    ollama_url: str

    def __post_init__(self):
        """Validate configuration."""
        if self.verify_cloud_timeout < 5:
            raise ValueError("verify_cloud_timeout must be >= 5s")
        if self.verify_local_timeout < 10:
            raise ValueError("verify_local_timeout must be >= 10s")
        if self.verify_async_timeout < 30:
            raise ValueError("verify_async_timeout must be >= 30s")
        if not (1 <= self.verify_job_retention <= 3600):
            raise ValueError("verify_job_retention must be 1-3600s")
        if self.verify_max_queue_size < 1:
            raise ValueError("verify_max_queue_size must be >= 1")
        if self.verify_max_concurrency < 1:
            raise ValueError("verify_max_concurrency must be >= 1")


def load_config() -> VerifyConfig:
    """Load and validate configuration from environment variables."""
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model = os.getenv(
        "ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"
    )
    verify_cloud_timeout = int(os.getenv("VERIFY_CLOUD_TIMEOUT", "45"))
    verify_local_timeout = int(os.getenv("VERIFY_LOCAL_TIMEOUT", "180"))
    verify_async_timeout = int(os.getenv("VERIFY_ASYNC_TIMEOUT", "300"))
    verify_job_retention = int(os.getenv("VERIFY_JOB_RETENTION", "300"))
    verify_max_queue_size = int(os.getenv("VERIFY_MAX_QUEUE_SIZE", "100"))
    verify_max_concurrency = int(os.getenv("VERIFY_MAX_CONCURRENCY", "2"))
    ollama_url = os.getenv("OLLAMA_URL", "")

    config = VerifyConfig(
        anthropic_api_key=anthropic_api_key,
        anthropic_model=anthropic_model,
        verify_cloud_timeout=verify_cloud_timeout,
        verify_local_timeout=verify_local_timeout,
        verify_async_timeout=verify_async_timeout,
        verify_job_retention=verify_job_retention,
        verify_max_queue_size=verify_max_queue_size,
        verify_max_concurrency=verify_max_concurrency,
        ollama_url=ollama_url,
    )

    log.info(
        "Verification config loaded: cloud_timeout=%ds, local_timeout=%ds, "
        "max_concurrency=%d, job_retention=%ds",
        verify_cloud_timeout,
        verify_local_timeout,
        verify_max_concurrency,
        verify_job_retention,
    )

    return config
