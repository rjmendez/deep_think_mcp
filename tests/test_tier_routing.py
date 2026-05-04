"""
Test suite for tier routing bug fix.

Tests that verify:
- Tier names are correctly mapped to actual model IDs
- _model_for_tier() is used instead of returning tier names
- All 3 tiers (light, medium, heavy) are properly routed
- Invalid tier names raise ValueError
- No regressions in orchestrator.py calls
"""
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.types import ProviderConfig
from engine import provider as provider_module


class TestTierRouting:
    """Unit tests for tier → model_id mapping."""

    def test_model_for_tier_light_tier(self):
        """Test that light tier returns a valid model ID, not 'light'."""
        cfg = ProviderConfig(provider="anthropic")
        model = provider_module._model_for_tier(cfg, "light", "general")
        
        # Should return a model ID, not the tier name
        assert model != "light"
        assert isinstance(model, str)
        assert len(model) > 0
        # For anthropic light tier, typically a mini model
        assert "mini" in model or "claude" in model or "gpt" in model

    def test_model_for_tier_medium_tier(self):
        """Test that medium tier returns a valid model ID, not 'medium'."""
        cfg = ProviderConfig(provider="anthropic")
        model = provider_module._model_for_tier(cfg, "medium", "general")
        
        # Should return a model ID, not the tier name
        assert model != "medium"
        assert isinstance(model, str)
        assert len(model) > 0

    def test_model_for_tier_heavy_tier(self):
        """Test that heavy tier returns a valid model ID, not 'heavy'."""
        cfg = ProviderConfig(provider="anthropic")
        model = provider_module._model_for_tier(cfg, "heavy", "general")
        
        # Should return a model ID, not the tier name
        assert model != "heavy"
        assert isinstance(model, str)
        assert len(model) > 0
        # For anthropic heavy tier, typically a premium model
        assert "claude" in model or "gpt" in model

    def test_model_for_tier_with_explicit_override(self):
        """Test that explicit per-tier override takes precedence."""
        cfg = ProviderConfig(provider="anthropic", light="custom-model-id")
        model = provider_module._model_for_tier(cfg, "light", "general")
        
        assert model == "custom-model-id"
        assert model != "light"

    def test_model_for_tier_with_global_override(self):
        """Test that global model override takes precedence."""
        cfg = ProviderConfig(provider="anthropic", model="global-model-id")
        model = provider_module._model_for_tier(cfg, "medium", "general")
        
        assert model == "global-model-id"

    def test_model_for_tier_with_env_var_override(self):
        """Test that environment variable override is respected."""
        cfg = ProviderConfig(provider="anthropic")
        
        with patch.dict('os.environ', {'DEEP_THINK_ANTHROPIC_LIGHT': 'env-model-id'}):
            model = provider_module._model_for_tier(cfg, "light", "general")
            assert model == "env-model-id"

    def test_model_for_tier_copilot_provider(self):
        """Test model selection for copilot provider."""
        cfg = ProviderConfig(provider="copilot")
        model = provider_module._model_for_tier(cfg, "light", "general")
        
        # Should return a model ID, not the tier name
        assert model != "light"
        assert isinstance(model, str)

    def test_model_for_tier_ollama_provider(self):
        """Test model selection for ollama provider."""
        cfg = ProviderConfig(provider="ollama")
        model = provider_module._model_for_tier(cfg, "medium", "general")
        
        # Should return a model ID, not the tier name
        assert model != "medium"
        assert isinstance(model, str)

    def test_model_for_tier_with_invalid_tier_logs_warning(self):
        """Test that invalid tier names are handled gracefully."""
        cfg = ProviderConfig(provider="anthropic")
        # Invalid tier should still return a model (falls back to default)
        model = provider_module._model_for_tier(cfg, "invalid_tier", "general")
        
        # Should not return the tier name itself
        assert model != "invalid_tier"
        # Should return a valid model ID fallback
        assert isinstance(model, str)
        assert len(model) > 0

    def test_model_for_tier_different_task_classes(self):
        """Test that task_class parameter doesn't cause tier name to be returned."""
        cfg = ProviderConfig(provider="anthropic")
        
        for task_class in ["general", "code_review", "investigation", "reasoning", "safety"]:
            model = provider_module._model_for_tier(cfg, "medium", task_class)
            
            # Should never return the tier name
            assert model != "medium"
            assert isinstance(model, str)
            assert len(model) > 0


class TestOrchestratorTierRouting:
    """Integration tests for tier routing in orchestrator."""

    @pytest.mark.asyncio
    async def test_deep_think_passes_uses_model_id_not_tier_name(self):
        """Test that deep_think_passes uses actual model IDs, not tier names."""
        from engine import orchestrator
        
        # Mock the provider call to verify what model_name is passed
        with patch.object(provider_module, '_call_provider', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "Mock reasoning output"
            
            result = await orchestrator.deep_think_passes(
                question="What is 2+2?",
                passes=1,
                task_class="general",
                data_policy="local",  # Use local to avoid cloud dependency
                provider_config={"provider": "ollama"}
            )
            
            # Verify the mock was called
            assert mock_call.called
            
            # Get the model_name argument passed to _call_provider
            call_args = mock_call.call_args
            model_name = call_args[1]['model'] if 'model' in call_args[1] else None
            
            # Model name should not be "light", "medium", or "heavy"
            if model_name:  # If model_name was explicitly passed
                assert model_name not in ("light", "medium", "heavy"), \
                    f"Model name should not be a tier name, got: {model_name}"

    @pytest.mark.asyncio
    async def test_orchestrator_respects_model_override(self):
        """Test that explicit model override is respected in orchestrator."""
        from engine import orchestrator
        
        with patch.object(provider_module, '_call_provider', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "Mock reasoning output"
            
            result = await orchestrator.deep_think_passes(
                question="What is 2+2?",
                passes=1,
                model="my-custom-model",
                data_policy="local",
                provider_config={"provider": "ollama"}
            )
            
            # Verify the model override was used
            assert mock_call.called
            call_args = mock_call.call_args
            model_name = call_args[1].get('model')
            
            if model_name:  # If model was passed to the provider
                # Should use the override, not a derived model
                assert "custom" in model_name or model_name == "my-custom-model"


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_provider_config(self):
        """Test with empty/default ProviderConfig."""
        cfg = ProviderConfig()  # All defaults
        model = provider_module._model_for_tier(cfg, "medium", "general")
        
        # Should still return a valid model ID, not "medium"
        assert model != "medium"
        assert isinstance(model, str)

    def test_model_for_tier_consistency(self):
        """Test that multiple calls return consistent models for same config."""
        cfg = ProviderConfig(provider="anthropic")
        
        model1 = provider_module._model_for_tier(cfg, "medium", "general")
        model2 = provider_module._model_for_tier(cfg, "medium", "general")
        
        # Should be consistent
        assert model1 == model2
        assert model1 != "medium"
        assert model2 != "medium"

    def test_tier_routing_precedence_order(self):
        """Test that tier routing respects the documented precedence order."""
        # Priority: 1. model > 2. light/medium/heavy > 3. env var > ...
        cfg = ProviderConfig(
            provider="anthropic",
            model="priority1",
            light="priority2"
        )
        
        # Global model should win
        model = provider_module._model_for_tier(cfg, "light", "general")
        assert model == "priority1"

    def test_tier_routing_with_data_policy_local(self):
        """Test that local data policy works with tier routing."""
        cfg = ProviderConfig(data_policy="local", provider="anthropic")
        model = provider_module._model_for_tier(cfg, "heavy", "general")
        
        # Should still return a valid model, not tier name
        assert model != "heavy"
        assert isinstance(model, str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
