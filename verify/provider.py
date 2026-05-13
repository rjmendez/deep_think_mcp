"""LLM provider abstraction for claim verification."""

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """Result of claim verification."""

    verdict: bool
    confidence: float
    reasoning: str
    latency_ms: float

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "latency_ms": self.latency_ms,
        }


class LLMProvider(ABC):
    """Abstract LLM provider for claim verification."""

    @abstractmethod
    async def verify_claim(
        self, claim: str, context: Optional[str] = None
    ) -> VerifyResult:
        """Verify a claim.

        Args:
            claim: Claim text to verify
            context: Optional context for grounding

        Returns:
            VerifyResult with verdict, confidence, and reasoning
        """
        pass


class CloudProvider(LLMProvider):
    """Anthropic Claude provider for claim verification."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-sonnet-20241022",
        timeout: int = 45,
    ):
        """Initialize cloud provider.

        Args:
            api_key: Anthropic API key
            model: Model ID (must start with 'claude-')
            timeout: Request timeout in seconds
        """
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY required for cloud provider")
        if not api_key.startswith("sk-ant"):
            raise ValueError("Invalid API key format (must start with 'sk-ant')")
        if not model.startswith("claude-"):
            raise ValueError("Invalid model (must be claude-*)")

        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = "https://api.anthropic.com/v1"

    async def verify_claim(
        self, claim: str, context: Optional[str] = None
    ) -> VerifyResult:
        """Verify claim using Anthropic Claude."""
        prompt = f"Verify this claim: {claim}"
        if context:
            prompt += f"\n\nContext: {context}"
        prompt += (
            "\n\nRespond with JSON: "
            '{"verdict": true/false, "confidence": 0.0-1.0, "reasoning": "..."}'
        )

        start_time = time.time()

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=self.timeout,
                )

                latency_ms = (time.time() - start_time) * 1000

                if response.status_code == 401:
                    raise ValueError("Invalid API key (401)")
                if response.status_code == 429:
                    raise ValueError("Rate limited (429)")
                if response.status_code >= 500:
                    raise ValueError(f"API error ({response.status_code})")

                response.raise_for_status()

                # Parse response
                data = response.json()
                content = data["content"][0]["text"]

                # Extract JSON from response
                try:
                    json_start = content.find("{")
                    json_end = content.rfind("}") + 1
                    json_str = content[json_start:json_end]
                    result = json.loads(json_str)

                    return VerifyResult(
                        verdict=bool(result.get("verdict", False)),
                        confidence=float(result.get("confidence", 0.5)),
                        reasoning=str(result.get("reasoning", "")),
                        latency_ms=latency_ms,
                    )
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    log.warning(
                        "Failed to parse Anthropic response: %s", e
                    )
                    return VerifyResult(
                        verdict=False,
                        confidence=0.0,
                        reasoning=f"Failed to parse response: {e}",
                        latency_ms=latency_ms,
                    )

            except httpx.TimeoutException:
                latency_ms = (time.time() - start_time) * 1000
                log.warning("Cloud verification timed out after %dms", latency_ms)
                raise TimeoutError(
                    f"Cloud verification timed out after {latency_ms}ms"
                )
            except Exception as e:
                latency_ms = (time.time() - start_time) * 1000
                log.error(
                    "Cloud verification failed after %dms: %s",
                    latency_ms,
                    e,
                )
                raise


class LocalProvider(LLMProvider):
    """Ollama provider for local claim verification."""

    def __init__(
        self,
        url: str = "",
        model: str = "neural-chat",
        timeout: int = 180,
    ):
        """Initialize local provider.

        Args:
            url: Ollama server URL
            model: Model name (default: neural-chat)
            timeout: Request timeout in seconds
        """
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def verify_claim(
        self, claim: str, context: Optional[str] = None
    ) -> VerifyResult:
        """Verify claim using local Ollama."""
        prompt = f"Verify this claim: {claim}"
        if context:
            prompt += f"\n\nContext: {context}"
        prompt += (
            "\n\nRespond with JSON: "
            '{"verdict": true/false, "confidence": 0.0-1.0, "reasoning": "..."}'
        )

        start_time = time.time()

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                    },
                    timeout=self.timeout,
                )

                latency_ms = (time.time() - start_time) * 1000

                response.raise_for_status()

                data = response.json()
                response_text = data.get("response", "")

                # Extract JSON from response
                try:
                    json_start = response_text.find("{")
                    json_end = response_text.rfind("}") + 1
                    json_str = response_text[json_start:json_end]
                    result = json.loads(json_str)

                    return VerifyResult(
                        verdict=bool(result.get("verdict", False)),
                        confidence=float(result.get("confidence", 0.5)),
                        reasoning=str(result.get("reasoning", "")),
                        latency_ms=latency_ms,
                    )
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    log.warning(
                        "Failed to parse Ollama response: %s", e
                    )
                    return VerifyResult(
                        verdict=False,
                        confidence=0.0,
                        reasoning=f"Failed to parse response: {e}",
                        latency_ms=latency_ms,
                    )

            except httpx.TimeoutException:
                latency_ms = (time.time() - start_time) * 1000
                log.warning("Local verification timed out after %dms", latency_ms)
                raise TimeoutError(
                    f"Local verification timed out after {latency_ms}ms"
                )
            except httpx.ConnectError:
                latency_ms = (time.time() - start_time) * 1000
                log.error(
                    "Cannot connect to Ollama at %s after %dms",
                    self.url,
                    latency_ms,
                )
                raise ConnectionError(
                    f"Cannot connect to Ollama at {self.url}"
                )
            except Exception as e:
                latency_ms = (time.time() - start_time) * 1000
                log.error(
                    "Local verification failed after %dms: %s",
                    latency_ms,
                    e,
                )
                raise
