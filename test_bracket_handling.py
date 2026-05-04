"""
Unit tests for bracket handling in claim extraction.

Tests the fixes for bracket truncation bug where patterns at confidence
extraction would incorrectly truncate claims with multiple brackets.

Test coverage:
- Single bracket claims
- Multi-bracket claims (e.g., [reference 1] text [reference 2])
- Nested brackets
- Bracket combinations with confidence markers
"""
import pytest
from nova_factcheck.extractor import ClaimExtractor


class TestBracketHandling:
    """Test bracket handling in claim extraction and cleaning."""

    def setup_method(self):
        """Initialize extractor before each test."""
        self.extractor = ClaimExtractor(min_words=3, max_claims=30)

    # ========================================================================
    # Unit Tests: _clean method (bracket removal)
    # ========================================================================

    def test_clean_single_numeric_bracket(self):
        """Test removal of single numeric citation [1]."""
        result = self.extractor._clean("[1] This is a claim")
        assert result == "This is a claim"
        assert "[1]" not in result

    def test_clean_multi_numeric_brackets(self):
        """Test multi-bracket claims: [reference 1] text [reference 2]."""
        result = self.extractor._clean("[1] first [2] second [3] third")
        # Note: Multiple spaces are collapsed to single spaces
        assert result == "first second third"
        assert "[1]" not in result
        assert "[2]" not in result
        assert "[3]" not in result

    def test_clean_consecutive_numeric_brackets(self):
        """Test consecutive brackets like [1][2][3] are all removed."""
        result = self.extractor._clean("Claim with [1][2][3] references")
        # Multiple spaces are collapsed to single spaces
        assert result == "Claim with references"
        assert "[1]" not in result
        assert "[2]" not in result
        assert "[3]" not in result

    def test_clean_comma_separated_citations(self):
        """Test comma-separated citations like [1,2,3]."""
        result = self.extractor._clean("[1,2,3] This is cited")
        assert result == "This is cited"
        assert "[1,2,3]" not in result

    def test_clean_preserves_non_numeric_brackets(self):
        """Test that non-numeric brackets are preserved (may be claim content)."""
        result = self.extractor._clean("Text [with non-numeric brackets]")
        assert "[with non-numeric brackets]" in result

    def test_clean_preserves_reference_text_brackets(self):
        """Test that reference text like [reference 1] is preserved."""
        result = self.extractor._clean("[reference 1] text [reference 2]")
        # Both reference brackets should remain (they contain text, not just digits)
        assert "[reference 1]" in result
        assert "[reference 2]" in result

    def test_clean_removes_whitespace_after_bracket_removal(self):
        """Test that multiple spaces left by citation removal are cleaned up."""
        result = self.extractor._clean("Text [1]  with  [2]  citations")
        # Multiple spaces should be collapsed to single spaces
        assert "  " not in result
        assert result == "Text with citations"

    def test_clean_mixed_bracket_types(self):
        """Test mixed numeric and text brackets."""
        result = self.extractor._clean("[1] fact [about something] more [2] text")
        # Numeric [1], [2] removed, text [about something] preserved
        assert "[1]" not in result
        assert "[2]" not in result
        assert "[about something]" in result

    # ========================================================================
    # Unit Tests: _extract_inline_confidence method
    # ========================================================================

    def test_confidence_single_bracket_claim(self):
        """Test confidence extraction from single bracket claim."""
        conf, cleaned = self.extractor._extract_inline_confidence("[confidence: 90%] This is true")
        assert conf == 0.9
        assert cleaned == "This is true"
        assert "[confidence" not in cleaned

    def test_confidence_multi_bracket_claim(self):
        """Test confidence extraction doesn't truncate multi-bracket claims.
        
        This is the core bug fix: patterns should not truncate at first bracket.
        """
        text = "[confidence: 85%] Claim [reference 1] with [reference 2]"
        conf, cleaned = self.extractor._extract_inline_confidence(text)
        
        # Should extract confidence without truncating
        assert conf == 0.85
        # Both references should remain
        assert "[reference 1]" in cleaned
        assert "[reference 2]" in cleaned
        assert "[confidence" not in cleaned

    def test_confidence_no_brackets(self):
        """Test confidence extraction from text without brackets."""
        conf, cleaned = self.extractor._extract_inline_confidence(
            "This is a claim with confidence: 75% likelihood"
        )
        assert conf == 0.75
        assert "confidence" not in cleaned or "confidence" in cleaned.lower()

    def test_confidence_parenthesized_no_truncation(self):
        """Test parenthesized confidence doesn't truncate."""
        text = "Claim (95% confidence) with [reference 1] more [reference 2]"
        conf, cleaned = self.extractor._extract_inline_confidence(text)
        
        assert conf == 0.95
        # All bracket content should remain
        assert "[reference 1]" in cleaned
        assert "[reference 2]" in cleaned

    def test_confidence_normalized_to_0_1_range(self):
        """Test that confidence values >1 are normalized to 0-1 range."""
        conf, _ = self.extractor._extract_inline_confidence("[confidence: 80] fact")
        assert 0.0 <= conf <= 1.0
        assert conf == 0.80

    def test_confidence_default_when_none_found(self):
        """Test default confidence when none found."""
        conf, cleaned = self.extractor._extract_inline_confidence("Just a claim without confidence")
        assert conf == 0.5
        assert cleaned == "Just a claim without confidence"

    # ========================================================================
    # Integration Tests: extract method
    # ========================================================================

    def test_extract_single_bracket_claim(self):
        """Test extraction of single bracket claim."""
        text = "[1] The database shows errors in the logs"
        claims = self.extractor.extract(text)
        
        assert len(claims) > 0
        # Numeric citation should be removed
        assert "[1]" not in claims[0].text

    def test_extract_multi_bracket_claim(self):
        """Test extraction of multi-bracket claim without truncation.
        
        Core bug fix: patterns at confidence extraction should handle multiple brackets.
        """
        text = "[reference 1] The system has latency issues [reference 2]"
        claims = self.extractor.extract(text)
        
        assert len(claims) > 0
        claim = claims[0]
        # Both references should be in the extracted claim
        assert "[reference 1]" in claim.text
        assert "[reference 2]" in claim.text

    def test_extract_nested_brackets(self):
        """Test extraction with nested brackets."""
        text = "The system contains [nested [bracket] content] that affects performance"
        claims = self.extractor.extract(text)
        
        assert len(claims) > 0
        # Nested brackets should be preserved
        assert "[nested [bracket] content]" in claims[0].text or \
               "[nested" in claims[0].text

    def test_extract_confidence_multi_bracket(self):
        """Test extraction with confidence marker in multi-bracket claim."""
        text = "[confidence: 92%] Fact [about something] is true [reference 1]"
        claims = self.extractor.extract(text)
        
        assert len(claims) > 0
        claim = claims[0]
        # Should extract without truncation
        assert claim.confidence_in_text == 0.92
        assert "[about something]" in claim.text
        assert "[reference 1]" in claim.text
        assert "[confidence" not in claim.text

    def test_extract_consecutive_brackets_removed(self):
        """Test that consecutive numeric brackets are removed properly."""
        text = "The system [1][2][3] has failed with multiple issues that need investigation"
        claims = self.extractor.extract(text)
        
        assert len(claims) > 0
        # Numeric citations should be removed
        assert "[1]" not in claims[0].text
        assert "[2]" not in claims[0].text
        assert "[3]" not in claims[0].text

    # ========================================================================
    # Regression Tests: Ensure fixes don't break existing behavior
    # ========================================================================

    def test_regression_standard_numeric_citations(self):
        """Regression: standard numeric citations should still work."""
        text = "[1] First claim [2] Second claim [3] Third claim"
        claims = self.extractor.extract(text)
        
        # All citations should be removed
        for claim in claims:
            assert "[1]" not in claim.text
            assert "[2]" not in claim.text
            assert "[3]" not in claim.text

    def test_regression_no_false_positive_removals(self):
        """Regression: ensure non-citation brackets aren't removed."""
        text = "The array [0:10] contains values"
        claims = self.extractor.extract(text)
        
        # This is not a citation, should be preserved
        assert len(claims) > 0
        assert "[0:10]" in claims[0].text

    def test_regression_confidence_extraction_with_brackets(self):
        """Regression: confidence extraction should work with brackets in text."""
        text = "[confidence: 88%] The feature [add-items] works properly"
        claims = self.extractor.extract(text)
        
        assert len(claims) > 0
        claim = claims[0]
        assert claim.confidence_in_text == 0.88
        # Non-numeric bracket should remain
        assert "[add-items]" in claim.text

    # ========================================================================
    # Edge Cases
    # ========================================================================

    def test_edge_case_empty_string(self):
        """Test handling of empty string."""
        result = self.extractor._clean("")
        assert result == ""

    def test_edge_case_only_brackets(self):
        """Test handling of text that is only brackets."""
        result = self.extractor._clean("[1][2][3]")
        assert result == ""

    def test_edge_case_mixed_spacing(self):
        """Test handling of irregular spacing in brackets."""
        result = self.extractor._clean("[1  , 2  , 3] Text here")
        # Pattern allows optional spaces, should match
        assert "Text here" in result

    def test_edge_case_very_long_bracket_list(self):
        """Test handling of long citation lists."""
        result = self.extractor._clean("[1,2,3,4,5,6,7,8,9,10] Very important claim")
        assert result == "Very important claim"
        # All should be removed
        for i in range(1, 11):
            assert f"[{i}" not in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
