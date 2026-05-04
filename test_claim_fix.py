"""Test suite for Claim dataclass constructor bug fix.

Verifies that:
1. Claim objects can be instantiated with correct fields
2. _extract_claims_from_pass_output generates proper claim dicts
3. All Claim instantiations match the dataclass signature
4. Type checking passes
"""

import pytest
from dataclasses import dataclass
from typing import Any

# Import the fixed module
from engine.orchestrator import (
    _extract_claims_from_pass_output,
    _build_claim_data,
    _extract_subject_from_statement,
)

# Import Claim dataclass
from ground_truth import Claim


class TestClaimDataclass:
    """Test Claim dataclass constructor with correct field names."""
    
    def test_claim_instantiation_correct_fields(self):
        """Verify Claim can be instantiated with correct field names."""
        claim = Claim(
            id="claim_001",
            statement="GPS position is valid",
            claim_type="telemetry_gps",
            subject="GPS.POSITION",
            expected_value={"valid_fix": True},
            confidence_model=0.95,
        )
        
        assert claim.id == "claim_001"
        assert claim.statement == "GPS position is valid"
        assert claim.claim_type == "telemetry_gps"
        assert claim.subject == "GPS.POSITION"
        assert claim.expected_value == {"valid_fix": True}
        assert claim.confidence_model == 0.95
    
    def test_claim_instantiation_missing_required_fields(self):
        """Verify Claim raises TypeError when required fields are missing."""
        with pytest.raises(TypeError):
            Claim(  # Missing statement, claim_type, subject, expected_value
                id="claim_001",
                confidence_model=0.95,
            )
    
    def test_claim_with_wrong_field_names(self):
        """Verify Claim raises TypeError with old field names (text, confidence, category)."""
        with pytest.raises(TypeError):
            Claim(  # Using old wrong field names
                text="GPS position is valid",
                confidence=0.95,
                category="telemetry_gps",
            )
    
    def test_claim_default_confidence_model(self):
        """Verify Claim has default value for confidence_model."""
        claim = Claim(
            id="claim_002",
            statement="Battery level is low",
            claim_type="device_metric",
            subject="battery",
            expected_value={"level": "low"},
        )
        
        assert claim.confidence_model == 0.5  # Default value


class TestClaimExtraction:
    """Test claim extraction produces valid Claim objects."""
    
    def test_extract_claims_from_pattern1(self):
        """Test extracting claims with CLAIM: [CONFIDENCE: X%] pattern."""
        output = "CLAIM: GPS position is stable [CONFIDENCE: 85%]"
        
        claims = _extract_claims_from_pass_output(output)
        
        assert len(claims) == 1
        assert claims[0]["statement"] == "GPS position is stable"
        assert claims[0]["confidence_model"] == 0.85
        assert claims[0]["claim_type"] == "inferred"
        assert "id" in claims[0]
        assert "subject" in claims[0]
        assert "expected_value" in claims[0]
    
    def test_extract_claims_from_pattern2_verified(self):
        """Test extracting claims with (✓) [N% confidence] pattern."""
        output = "(✓) Database connection is responsive [90% confidence]"
        
        claims = _extract_claims_from_pass_output(output)
        
        assert len(claims) == 1
        assert claims[0]["statement"] == "Database connection is responsive"
        assert claims[0]["confidence_model"] == 0.90
        assert claims[0]["claim_type"] == "verified"
    
    def test_extract_claims_from_pattern2_refuted(self):
        """Test extracting claims with (✗) [N% confidence] pattern."""
        output = "(✗) Memory usage is below 50% [30% confidence]"
        
        claims = _extract_claims_from_pass_output(output)
        
        assert len(claims) == 1
        assert claims[0]["statement"] == "Memory usage is below 50%"
        assert claims[0]["confidence_model"] == 0.30
        assert claims[0]["claim_type"] == "refuted"
    
    def test_extract_claims_multiple(self):
        """Test extracting multiple claims from output."""
        output = """CLAIM: GPS position is valid [CONFIDENCE: 95%]
        (✓) Network connection established [85% confidence]
        CLAIM: Battery level is adequate [CONFIDENCE: 70%]"""
        
        claims = _extract_claims_from_pass_output(output)
        
        assert len(claims) == 3
        # All should have required fields
        for claim in claims:
            assert "id" in claim
            assert "statement" in claim
            assert "claim_type" in claim
            assert "subject" in claim
            assert "expected_value" in claim
            assert "confidence_model" in claim
    
    def test_extracted_claims_instantiate_to_claim_objects(self):
        """Test that extracted claim dicts can be used to create Claim objects."""
        output = "CLAIM: Database connection works [CONFIDENCE: 92%]"
        
        claim_dicts = _extract_claims_from_pass_output(output)
        assert len(claim_dicts) == 1
        
        # Should be able to instantiate Claim objects from extracted dicts
        claim_dict = claim_dicts[0]
        claim_obj = Claim(
            id=claim_dict["id"],
            statement=claim_dict["statement"],
            claim_type=claim_dict["claim_type"],
            subject=claim_dict["subject"],
            expected_value=claim_dict["expected_value"],
            confidence_model=claim_dict["confidence_model"],
        )
        
        assert isinstance(claim_obj, Claim)
        assert claim_obj.statement == "Database connection works"
        assert claim_obj.confidence_model == 0.92


class TestClaimDataBuilding:
    """Test _build_claim_data function."""
    
    def test_build_claim_data_basic(self):
        """Test building basic claim data."""
        data = _build_claim_data(
            statement="GPS position is valid",
            confidence_model=0.85,
            claim_type="telemetry_gps",
            claim_id=0,
        )
        
        assert data["statement"] == "GPS position is valid"
        assert data["confidence_model"] == 0.85
        assert data["claim_type"] == "telemetry_gps"
        assert "id" in data
        assert "subject" in data
        assert "expected_value" in data
    
    def test_build_claim_data_clamps_confidence(self):
        """Test that confidence_model is clamped to [0, 1]."""
        data_high = _build_claim_data(
            statement="Test",
            confidence_model=1.5,
            claim_type="test",
            claim_id=0,
        )
        assert data_high["confidence_model"] == 1.0
        
        data_low = _build_claim_data(
            statement="Test",
            confidence_model=-0.5,
            claim_type="test",
            claim_id=0,
        )
        assert data_low["confidence_model"] == 0.0


class TestSubjectExtraction:
    """Test _extract_subject_from_statement function."""
    
    def test_extract_subject_known_keyword_lowercase(self):
        """Test extracting known keyword even when lowercase."""
        subject = _extract_subject_from_statement("the cpu temperature is high")
        assert subject == "CPU"
    
    def test_extract_subject_capitalized_noun(self):
        """Test extracting subject from capitalized nouns."""
        subject = _extract_subject_from_statement("GPS position is stable at coordinates")
        assert subject == "GPS"
    
    def test_extract_subject_known_keyword_uppercase(self):
        """Test extracting subject from known technical keywords."""
        subject = _extract_subject_from_statement("The CPU temperature is high")
        assert subject == "CPU"
    
    def test_extract_subject_first_significant_word(self):
        """Test extracting subject from first significant word."""
        subject = _extract_subject_from_statement("database connection timeout occurred")
        assert subject == "database"
    
    def test_extract_subject_with_punctuation(self):
        """Test extracting subject that has punctuation."""
        subject = _extract_subject_from_statement("API's response time increased")
        assert subject == "API"
    
    def test_extract_subject_unknown(self):
        """Test returning 'unknown' when no good subject found."""
        subject = _extract_subject_from_statement("a b c")
        assert subject == "unknown"


class TestClaimValidation:
    """Integration tests for claim validation pipeline."""
    
    def test_end_to_end_claim_creation(self):
        """Test end-to-end: extract -> build -> instantiate Claim."""
        output = "CLAIM: Battery level is adequate [CONFIDENCE: 78%]"
        
        # Step 1: Extract claims from output
        claim_dicts = _extract_claims_from_pass_output(output)
        assert len(claim_dicts) == 1
        
        # Step 2: Instantiate Claim object
        claim_dict = claim_dicts[0]
        claim_obj = Claim(
            id=claim_dict["id"],
            statement=claim_dict["statement"],
            claim_type=claim_dict["claim_type"],
            subject=claim_dict["subject"],
            expected_value=claim_dict["expected_value"],
            confidence_model=claim_dict["confidence_model"],
        )
        
        # Step 3: Verify all fields
        assert isinstance(claim_obj, Claim)
        assert claim_obj.statement == "Battery level is adequate"
        assert claim_obj.confidence_model == 0.78
        assert claim_obj.claim_type == "inferred"
        assert len(claim_obj.id) > 0
    
    def test_multiple_claims_all_valid(self):
        """Test that all extracted claims are valid Claim objects."""
        output = """
        CLAIM: GPS is locked [CONFIDENCE: 92%]
        (✓) Network is responsive [88% confidence]
        (✗) Memory is low [45% confidence]
        CLAIM: Sensor data is fresh [CONFIDENCE: 85%]
        """
        
        claim_dicts = _extract_claims_from_pass_output(output)
        
        # Each extracted claim should be convertible to Claim object
        claim_objects = []
        for claim_dict in claim_dicts:
            claim_obj = Claim(
                id=claim_dict["id"],
                statement=claim_dict["statement"],
                claim_type=claim_dict["claim_type"],
                subject=claim_dict["subject"],
                expected_value=claim_dict["expected_value"],
                confidence_model=claim_dict["confidence_model"],
            )
            claim_objects.append(claim_obj)
        
        # All should be created successfully
        assert len(claim_objects) == 4
        for claim in claim_objects:
            assert isinstance(claim, Claim)
            assert all(hasattr(claim, field) 
                      for field in ["id", "statement", "claim_type", "subject", 
                                   "expected_value", "confidence_model"])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
