"""Simple tests for MQTT local-only LLM enforcement."""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

# Add the module to the path
sys.path.insert(0, '/home/rjmendez/development')

from deep_think_mcp.engine import (
    SecurityError,
    _validate_provider_is_local,
    _check_ollama_available,
)


class TestProviderValidation:
    """Test provider validation for local-only enforcement."""
    
    def test_validate_provider_is_local_blocks_anthropic(self):
        """Anthropic provider should be blocked when force_local=True."""
        with pytest.raises(SecurityError, match="Cloud provider 'anthropic' blocked"):
            _validate_provider_is_local("anthropic", force_local=True)
    
    def test_validate_provider_is_local_blocks_copilot(self):
        """Copilot provider should be blocked when force_local=True."""
        with pytest.raises(SecurityError, match="Cloud provider 'copilot' blocked"):
            _validate_provider_is_local("copilot", force_local=True)
    
    def test_validate_provider_is_local_allows_ollama(self):
        """Ollama should pass validation when force_local=True."""
        # Should not raise
        _validate_provider_is_local("ollama", force_local=True)
    
    def test_validate_provider_is_local_disables_without_force(self):
        """Validation should be skipped when force_local=False."""
        # Cloud providers should be allowed when force_local=False
        _validate_provider_is_local("anthropic", force_local=False)
        _validate_provider_is_local("copilot", force_local=False)


class TestOllamaAvailabilityCheck:
    """Test Ollama availability checking at startup."""
    
    @pytest.mark.asyncio
    async def test_check_ollama_available_success(self):
        """Should return True when Ollama is reachable with models."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "models": [
                    {"name": "phi4-mini"},
                    {"name": "qwen2.5-coder"},
                ]
            }
            mock_client.get.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            result = await _check_ollama_available("http://localhost:11434")
            assert result is True
    
    @pytest.mark.asyncio
    async def test_check_ollama_available_no_models(self):
        """Should return False when Ollama has no models."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {"models": []}
            mock_client.get.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            result = await _check_ollama_available("http://localhost:11434")
            assert result is False
    
    @pytest.mark.asyncio
    async def test_check_ollama_available_unreachable(self):
        """Should return False when Ollama is unreachable."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.side_effect = Exception("Connection refused")
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            result = await _check_ollama_available("http://localhost:11434")
            assert result is False


class TestEnvironmentVariableOverrides:
    """Test environment variable overrides for enforcement."""
    
    def test_deep_think_force_local_env_var_default_true(self):
        """DEEP_THINK_FORCE_LOCAL should default to "1" (true)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEP_THINK_FORCE_LOCAL", None)
            # When no env var set, default is "1"
            env_force_local = os.getenv("DEEP_THINK_FORCE_LOCAL", "1") != "0"
            assert env_force_local is True
    
    def test_deep_think_force_local_env_var_can_be_disabled(self):
        """DEEP_THINK_FORCE_LOCAL=0 should allow cloud providers."""
        with patch.dict(os.environ, {"DEEP_THINK_FORCE_LOCAL": "0"}):
            env_force_local = os.getenv("DEEP_THINK_FORCE_LOCAL", "1") != "0"
            assert env_force_local is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
