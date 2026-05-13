"""Unit tests for mqtt_integration module.

Tests MQTTConfig, MQTTClaimsProcessor, and lifecycle management.
No real MQTT broker or DAMAColonySubscriber needed—uses mocks.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import modules to test
from mqtt.config import MQTTConfig
from mqtt.subscriber import (
    MQTTClaimsProcessor,
    mqtt_startup,
    mqtt_shutdown,
    get_mqtt_processor,
    get_mqtt_subscriber,
    is_mqtt_enabled,
)


class TestMQTTConfig:
    """Test MQTT configuration loading and validation."""
    
    def test_config_defaults(self) -> None:
        """Test default configuration values."""
        with patch.dict(os.environ, {}, clear=False):
            # Clear MQTT env vars to get defaults
            for key in list(os.environ.keys()):
                if key.startswith("MQTT_"):
                    del os.environ[key]
            
            config = MQTTConfig()
            
            assert config.enabled == False  # Default disabled
            assert config.broker_host == "localhost"
            assert config.broker_port == 1883
            assert config.broker_user == "dama"
            assert config.batch_size == 10
            assert config.queue_size == 1000
            assert config.batch_timeout_sec == 5.0
    
    def test_config_from_environment(self) -> None:
        """Test loading configuration from environment variables."""
        env_vars = {
            "MQTT_ENABLE": "true",
            "MQTT_HOST": "example.com",
            "MQTT_PORT": "8883",
            "MQTT_USERNAME": "user123",
            "MQTT_PASSWORD": "pass456",
            "MQTT_USE_TLS": "true",
            "MQTT_SUBSCRIBER_QUEUE_SIZE": "500",
            "MQTT_BATCH_SIZE": "20",
            "MQTT_BATCH_TIMEOUT_MS": "3000",
        }
        
        with patch.dict(os.environ, env_vars):
            config = MQTTConfig()
            
            assert config.enabled == True
            assert config.broker_host == "example.com"
            assert config.broker_port == 8883
            assert config.broker_user == "user123"
            assert config.broker_password == "pass456"
            assert config.use_tls == True
            assert config.queue_size == 500
            assert config.batch_size == 20
            assert config.batch_timeout_sec == 3.0
    
    def test_config_validation_ok(self) -> None:
        """Test valid configuration passes validation."""
        with patch.dict(os.environ, {"MQTT_ENABLE": "true"}):
            config = MQTTConfig()
            error = config.validate()
            assert error is None
    
    def test_config_validation_disabled(self) -> None:
        """Test disabled MQTT passes validation."""
        with patch.dict(os.environ, {"MQTT_ENABLE": "false"}):
            config = MQTTConfig()
            error = config.validate()
            assert error is None
    
    def test_config_validation_port_out_of_range(self) -> None:
        """Test invalid port fails validation."""
        with patch.dict(os.environ, {
            "MQTT_ENABLE": "true",
            "MQTT_PORT": "99999",
        }):
            config = MQTTConfig()
            error = config.validate()
            assert error is not None
            assert "MQTT_PORT" in error
    
    def test_config_validation_invalid_batch_size(self) -> None:
        """Test invalid batch size fails validation."""
        with patch.dict(os.environ, {
            "MQTT_ENABLE": "true",
            "MQTT_BATCH_SIZE": "0",
        }):
            config = MQTTConfig()
            error = config.validate()
            assert error is not None
            assert "MQTT_BATCH_SIZE" in error
    
    def test_config_repr(self) -> None:
        """Test config string representation hides password."""
        with patch.dict(os.environ, {
            "MQTT_ENABLE": "true",
            "MQTT_PASSWORD": "secret123",
        }):
            config = MQTTConfig()
            repr_str = repr(config)
            
            assert "secret123" not in repr_str
            assert "localhost" in repr_str
            assert "enabled=True" in repr_str


class TestMQTTClaimsProcessor:
    """Test MQTTClaimsProcessor batching and processing."""
    
    @pytest.fixture
    def config(self) -> MQTTConfig:
        """Create test configuration."""
        with patch.dict(os.environ, {
            "MQTT_ENABLE": "true",
            "MQTT_BATCH_SIZE": "3",
            "MQTT_BATCH_TIMEOUT_MS": "1000",
        }):
            return MQTTConfig()
    
    @pytest.fixture
    def mock_subscriber(self) -> AsyncMock:
        """Create mock subscriber."""
        subscriber = AsyncMock()
        subscriber._mqtt_client = AsyncMock()
        return subscriber
    
    @pytest.fixture
    def processor(self, config: MQTTConfig, mock_subscriber: AsyncMock) -> MQTTClaimsProcessor:
        """Create test processor."""
        return MQTTClaimsProcessor(config, mock_subscriber)
    
    @pytest.mark.asyncio
    async def test_processor_init(self, processor: MQTTClaimsProcessor) -> None:
        """Test processor initialization."""
        assert processor._processed_count == 0
        assert processor._error_count == 0
        assert len(processor._batch_buffer) == 0
        assert not processor._running
    
    @pytest.mark.asyncio
    async def test_processor_start_stop(self, processor: MQTTClaimsProcessor) -> None:
        """Test processor start and stop."""
        # Mock the processor loop to avoid infinite loop
        with patch.object(processor, '_run_processor_loop', new_callable=AsyncMock):
            await processor.start()
            assert processor._running == True
            assert processor._processor_task is not None
            
            await processor.stop()
            assert processor._running == False
    
    @pytest.mark.asyncio
    async def test_processor_no_subscriber(self) -> None:
        """Test processor fails gracefully without subscriber."""
        config = MQTTConfig()
        processor = MQTTClaimsProcessor(config, subscriber=None)
        
        # Should not crash
        await processor.start()  # Should exit early
        await processor.stop()
    
    @pytest.mark.asyncio
    async def test_collect_batch_empty_queue(self, processor: MQTTClaimsProcessor) -> None:
        """Test collecting batch from empty queue returns empty list."""
        processor.subscriber.get_claim.return_value = None
        
        batch = await processor._collect_batch()
        assert batch == []
    
    @pytest.mark.asyncio
    async def test_collect_batch_partial_fill(self, processor: MQTTClaimsProcessor) -> None:
        """Test collecting batch stops at timeout if batch not full."""
        mock_claims = [
            MagicMock(id="claim1", statement="test1"),
            MagicMock(id="claim2", statement="test2"),
        ]
        processor.subscriber.get_claim.side_effect = [
            mock_claims[0],
            mock_claims[1],
            None,  # Timeout
        ]
        
        batch = await processor._collect_batch()
        assert len(batch) == 2
        assert batch[0].id == "claim1"
        assert batch[1].id == "claim2"
    
    @pytest.mark.asyncio
    async def test_extract_findings_valid_json(self, processor: MQTTClaimsProcessor) -> None:
        """Test extracting findings from valid deep_think result."""
        result_json = json.dumps({
            "final_answer": "Test answer",
            "confidence": 0.95,
            "passes": 3,
            "claims": [
                {"claim": "claim1", "confidence": 0.9},
            ],
        })
        
        findings = processor._extract_findings(result_json, "device123")
        
        assert findings["device_id"] == "device123"
        assert findings["final_answer"] == "Test answer"
        assert findings["confidence"] == 0.95
        assert findings["passes"] == 3
        assert "claims" in findings
    
    def test_extract_findings_invalid_json(self, processor: MQTTClaimsProcessor) -> None:
        """Test extracting findings from invalid JSON gracefully."""
        invalid_json = "not valid json {]"
        
        findings = processor._extract_findings(invalid_json, "device123")
        
        assert findings["device_id"] == "device123"
        assert "error" in findings
        assert findings["confidence"] == 0.0
    
    @pytest.mark.asyncio
    async def test_publish_findings_no_client(self, processor: MQTTClaimsProcessor) -> None:
        """Test publish gracefully handles missing MQTT client."""
        processor.subscriber._mqtt_client = None
        
        # Should not crash
        await processor._publish_findings({"test": "data"}, "device123")
    
    @pytest.mark.asyncio
    async def test_publish_findings_success(self, processor: MQTTClaimsProcessor) -> None:
        """Test successful findings publication."""
        findings = {
            "device_id": "device123",
            "final_answer": "Test",
            "confidence": 0.9,
        }
        
        await processor._publish_findings(findings, "device123")
        
        # Verify publish was called
        processor.subscriber._mqtt_client.publish.assert_called_once()
        call_args = processor.subscriber._mqtt_client.publish.call_args
        
        assert "dama/colony/findings/device123" in call_args[0][0]
        assert "Test" in call_args[0][1]


class TestLifecycleManagement:
    """Test lifecycle hooks and signal handling."""
    
    @pytest.mark.asyncio
    async def test_mqtt_startup_disabled(self) -> None:
        """Test startup when MQTT is disabled."""
        with patch.dict(os.environ, {"MQTT_ENABLE": "false"}):
            await mqtt_startup()
            
            # Should complete without error but not initialize
            assert is_mqtt_enabled() == False or get_mqtt_subscriber() is None
    
    @pytest.mark.asyncio
    async def test_mqtt_shutdown_no_processor(self) -> None:
        """Test shutdown handles no processor gracefully."""
        # Should not crash
        await mqtt_shutdown()


class TestConfigIntegration:
    """Test configuration with real environment."""
    
    def test_dotenv_loading(self) -> None:
        """Test .env configuration is loaded correctly."""
        # Load actual .env
        config = MQTTConfig()
        
        # Should load from .env without error
        assert config.broker_host == "localhost"
        assert config.broker_port == 1883
        assert config.broker_user == "dama"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
