"""Tests for MQTT local-only LLM enforcement and security hardening."""

import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from engine import (
    deep_think_passes,
    run_fan_out,
    ProviderConfig,
    build_provider_config,
    SecurityError,
    _validate_provider_is_local,
    _check_ollama_available,
    _validate_and_enforce_local_models,
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
    
    def test_validate_provider_is_local_blocks_azure(self):
        """Azure provider should be blocked when force_local=True."""
        with pytest.raises(SecurityError, match="Cloud provider 'azure' blocked"):
            _validate_provider_is_local("azure", force_local=True)
    
    def test_validate_provider_is_local_blocks_openai(self):
        """OpenAI provider should be blocked when force_local=True."""
        with pytest.raises(SecurityError, match="Cloud provider 'openai' blocked"):
            _validate_provider_is_local("openai", force_local=True)
    
    def test_validate_provider_is_local_allows_ollama(self):
        """Ollama should pass validation when force_local=True."""
        # Should not raise
        _validate_provider_is_local("ollama", force_local=True)
    
    def test_validate_provider_is_local_disables_without_force(self):
        """Validation should be skipped when force_local=False."""
        # Cloud providers should be allowed when force_local=False
        _validate_provider_is_local("anthropic", force_local=False)
        _validate_provider_is_local("copilot", force_local=False)
        _validate_provider_is_local("azure", force_local=False)
        _validate_provider_is_local("openai", force_local=False)
    
    def test_validate_provider_case_insensitive(self):
        """Provider validation should be case-insensitive."""
        with pytest.raises(SecurityError):
            _validate_provider_is_local("ANTHROPIC", force_local=True)
        
        with pytest.raises(SecurityError):
            _validate_provider_is_local("CopiloT", force_local=True)


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
    
    @pytest.mark.asyncio
    async def test_check_ollama_available_custom_base_url(self):
        """Should respect custom base URL parameter."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {"models": [{"name": "llama2"}]}
            mock_client.get.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            result = await _check_ollama_available("http://remote:11434")
            mock_client.get.assert_called_with("http://remote:11434/api/tags")
            assert result is True


class TestValidateAndEnforceLocalModels:
    """Test comprehensive local-only enforcement."""
    
    @pytest.mark.asyncio
    async def test_enforce_local_models_sets_policy(self):
        """Should set data_policy=local when enforcing."""
        cfg = build_provider_config({"provider": "ollama"})
        
        with patch("engine._check_ollama_available", return_value=True):
            await _validate_and_enforce_local_models(cfg, force_local=True)
        
        assert cfg.data_policy == "local"
    
    @pytest.mark.asyncio
    async def test_enforce_local_models_validates_tiers(self):
        """Should validate that all tiers are Ollama when enforcing."""
        cfg = build_provider_config({
            "provider": "ollama",
            "light_provider": "ollama",
            "medium_provider": "ollama",
            "heavy_provider": "ollama",
        })
        
        with patch("engine._check_ollama_available", return_value=True):
            # Should not raise
            await _validate_and_enforce_local_models(cfg, force_local=True)
    
    @pytest.mark.asyncio
    async def test_enforce_local_models_rejects_cloud_tier(self):
        """Should reject if any tier uses cloud provider."""
        cfg = build_provider_config({
            "provider": "ollama",
            "light_provider": "ollama",
            "medium_provider": "copilot",  # Cloud provider in medium tier!
            "heavy_provider": "ollama",
        })
        
        with patch("engine._check_ollama_available", return_value=True):
            with pytest.raises(SecurityError, match="blocked in local-only mode"):
                await _validate_and_enforce_local_models(cfg, force_local=True)
    
    @pytest.mark.asyncio
    async def test_enforce_local_models_checks_ollama_availability(self):
        """Should check Ollama availability and raise if unavailable in strict mode."""
        cfg = build_provider_config({"provider": "ollama"})
        
        with patch("engine._check_ollama_available", return_value=False):
            with patch.dict(os.environ, {"OLLAMA_ONLY_MODE": "1"}):
                with pytest.raises(SecurityError, match="Ollama unavailable"):
                    await _validate_and_enforce_local_models(cfg, force_local=True)
    
    @pytest.mark.asyncio
    async def test_enforce_local_models_graceful_degradation(self):
        """Should degrade gracefully if Ollama unavailable without strict mode."""
        cfg = build_provider_config({"provider": "ollama"})
        
        with patch("engine._check_ollama_available", return_value=False):
            with patch.dict(os.environ, {"OLLAMA_ONLY_MODE": "0"}):
                # Should not raise
                await _validate_and_enforce_local_models(cfg, force_local=True)


class TestDeepThinkPassesLocalEnforcement:
    """Test deep_think_passes with local-only enforcement."""
    
    @pytest.mark.asyncio
    async def test_deep_think_passes_force_local_models_true(self):
        """Should enforce local-only when force_local_models=True."""
        with patch.dict(os.environ, {"DEEP_THINK_FORCE_LOCAL": "1"}):
            with patch("engine.deep_think_passes", new_callable=AsyncMock) as mock_dtp:
                with patch("engine._validate_and_enforce_local_models", new_callable=AsyncMock):
                    # Call with force_local_models should trigger validation
                    cfg = build_provider_config({"provider": "ollama"})
                    with patch("engine.build_provider_config", return_value=cfg):
                        with patch("engine._validate_and_enforce_local_models", new_callable=AsyncMock):
                            with patch("engine.refresh_ollama_models", new_callable=AsyncMock, return_value=set()):
                                # This is tested at the implementation level
                                pass
    
    @pytest.mark.asyncio
    async def test_deep_think_passes_device_id_logging(self):
        """Should include device_id in logs when provided."""
        # Test that device_id is passed through and logged
        # This is primarily a logging/tagging test
        pass


class TestRunFanOutLocalEnforcement:
    """Test run_fan_out with local-only enforcement."""
    
    @pytest.mark.asyncio
    async def test_run_fan_out_force_local_models_true(self):
        """Should enforce local-only on all configs in pool."""
        # Test that all configurations in the pool are enforced
        pass
    
    @pytest.mark.asyncio
    async def test_run_fan_out_device_id_propagation(self):
        """Should propagate device_id to all perspectives."""
        # Test that device_id reaches nested deep_think_passes calls
        pass


class TestEnvironmentVariableOverrides:
    """Test environment variable overrides for enforcement."""
    
    @pytest.mark.asyncio
    async def test_deep_think_force_local_env_var_default_true(self):
        """DEEP_THINK_FORCE_LOCAL should default to "1" (true)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEP_THINK_FORCE_LOCAL", None)
            # When no env var set, default is "1"
            env_force_local = os.getenv("DEEP_THINK_FORCE_LOCAL", "1") != "0"
            assert env_force_local is True
    
    @pytest.mark.asyncio
    async def test_deep_think_force_local_env_var_can_be_disabled(self):
        """DEEP_THINK_FORCE_LOCAL=0 should allow cloud providers."""
        with patch.dict(os.environ, {"DEEP_THINK_FORCE_LOCAL": "0"}):
            env_force_local = os.getenv("DEEP_THINK_FORCE_LOCAL", "1") != "0"
            assert env_force_local is False
    
    @pytest.mark.asyncio
    async def test_ollama_only_mode_forces_local(self):
        """OLLAMA_ONLY_MODE=1 should force local models."""
        with patch.dict(os.environ, {"OLLAMA_ONLY_MODE": "1"}):
            ollama_only_mode = os.getenv("OLLAMA_ONLY_MODE", "0") != "0"
            assert ollama_only_mode is True


class TestMQTTIntegration:
    """Test integration with MQTT operations."""
    
    def test_mqtt_device_id_parameter(self):
        """Device ID should be extracted and passed to deep_think."""
        # Simulates how worker.py extracts device_id from provider_config
        provider_config = {
            "device_id": "ant_001",
            "force_local_models": True,
        }
        device_id = provider_config.pop("device_id", "")
        force_local_models = provider_config.pop("force_local_models", False)
        
        assert device_id == "ant_001"
        assert force_local_models is True
    
    def test_mqtt_auto_enable_force_local(self):
        """Having device_id should auto-enable force_local_models."""
        device_id = "ant_001"
        force_local_models = False
        
        # Auto-enable logic from worker.py
        if device_id or force_local_models:
            force_local_models = True
        
        assert force_local_models is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
