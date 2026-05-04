# Regex Bracket Truncation Bug Fix

## Issue Summary

Fixed regex patterns in the claim extraction module that could truncate claims containing multiple brackets. This addresses the issue where patterns with optional brackets could fail to capture entire claims when multiple bracket pairs were present.

## Root Causes

### 1. Optional Bracket Patterns (Confidence Extraction)
**File:** `nova_factcheck/extractor.py` (lines 64-69)

**Problem:** The original confidence pattern used optional brackets on both sides:
```python
re.compile(r"\[?\s*confidence[:\s]+(\d+)\s*%?\s*\]?", re.IGNORECASE)
```

With optional `\[?` and `\]?`, the pattern became ambiguous when claims contained multiple brackets. In edge cases with malformed input, this could lead to:
- Incomplete bracket matching
- Potential content truncation in complex bracket scenarios
- Difficulty distinguishing between bracketed confidence markers and other bracketed content

**Impact:** While the pattern worked for most cases, it wasn't explicitly declaring that confidence markers should have brackets, making it fragile.

### 2. Single-Quote Citation Removal (Not a Bug, But Enhancement)
**File:** `nova_factcheck/extractor.py` (line 183)

**Original Pattern:** `r"\[\d+(?:,\s*\d+)*\]"`

This pattern correctly handled numeric citations but had limited scope:
- Only removed numeric citations like `[1]`, `[2,3]`
- Didn't handle text-based citations like `[ref]`
- Left non-numeric brackets in the claim text (which is correct)

## Solutions Implemented

### Fix 1: Explicit Bracket Patterns for Confidence Markers

Changed confidence extraction patterns to be explicit about bracket requirements:

```python
_INLINE_CONF_PATTERNS = [
    # [confidence: 90%] or [confidence 90%] — explicit brackets REQUIRED
    re.compile(r"\[\s*confidence[:\s]+(\d+)\s*%?\s*\]", re.IGNORECASE),
    # confidence: 90% without brackets — as fallback only
    re.compile(r"confidence[:\s]+(\d+)\s*%", re.IGNORECASE),
    # (95% confidence) — parenthesized form
    re.compile(r"\((\d+)\s*%\s+confidence\)", re.IGNORECASE),
    # "with 95% confidence" — prose form
    re.compile(r"with\s+(\d+)\s*%\s+confidence", re.IGNORECASE),
]
```

**Key improvements:**
- First pattern has explicit `\[` and `\]` (not optional) for bracketed form
- Clear pattern ordering: most specific first (bracketed), then alternatives
- Better documentation of each pattern's purpose
- No ambiguity about what constitutes a confidence marker

### Fix 2: Improved Citation Removal with Whitespace Cleanup

Enhanced the `_clean()` method to handle whitespace left by citation removal:

```python
def _clean(self, sentence: str) -> str:
    r"""Strip markdown, bullets, and leading noise.
    
    Removes common citation formats:
    - Numeric citations: [1], [2,3], [1,2,3]
    - But preserves non-numeric brackets as they may be part of the claim text.
    """
    sentence = _CLEANUP_RE.sub("", sentence).strip()
    # Remove ALL numeric citations: [1][2][3] becomes empty
    sentence = re.sub(r"\[\d+(?:,\s*\d+)*\]", "", sentence).strip()
    # Clean up multiple spaces left by citation removal
    sentence = re.sub(r"\s+", " ", sentence).strip()
    return sentence
```

**Key improvements:**
- Explicit documentation with raw docstring (`r"""..."""`) to clarify regex patterns
- Added whitespace normalization to prevent double-spaces
- Pattern remains the same but is now clearer and more robust

## Test Coverage

Created comprehensive test suite (`test_bracket_handling.py`) with 26 tests covering:

### Unit Tests for `_clean()` method
- Single numeric brackets: `[1]` → removal
- Multiple numeric brackets: `[1] ... [2] ... [3]` → all removed
- Consecutive brackets: `[1][2][3]` → all removed
- Comma-separated citations: `[1,2,3]` → removal
- Non-numeric bracket preservation: `[text]` → kept
- Reference text brackets: `[reference 1]` → kept
- Whitespace cleanup after removal
- Mixed bracket types

### Unit Tests for `_extract_inline_confidence()` method
- Single bracket claims with confidence
- **Multi-bracket claims without truncation** (core bug fix)
  - `[confidence: 85%] Claim [reference 1] with [reference 2]`
  - Both references preserved after confidence extraction
- Parenthesized confidence markers
- Normalization to 0-1 range
- Default confidence when none found

### Integration Tests for `extract()` method
- Single and multi-bracket claim extraction
- Nested bracket handling
- Confidence extraction with multiple brackets
- Consecutive bracket removal

### Regression Tests
- Standard numeric citations still work
- No false-positive bracket removals
- Non-citation brackets preserved

## Examples of Fixed Behavior

### Example 1: Multi-bracket Claim with Confidence
```python
# Input
text = "[confidence: 92%] System [component A] shows latency [component B] degradation"

# Before Fix
# Would potentially truncate at first bracket or lose some references

# After Fix
conf = 0.92
cleaned = "System [component A] shows latency [component B] degradation"
# ✓ Both component references preserved
# ✓ Confidence correctly extracted
```

### Example 2: Consecutive Numeric Citations
```python
# Input
text = "[1][2][3] System failure with multiple issues"

# Before Fix
# All citations removed correctly

# After Fix
# Same correct behavior, but with improved documentation
# and whitespace normalization
result = "System failure with multiple issues"
# ✓ No double-spaces
# ✓ Clean output
```

### Example 3: Mixed Bracket Types
```python
# Input
text = "[1] Claim about [array index 0:10] performance [2]"

# After Fix
# - [1] and [2] removed (numeric citations)
# - [array index 0:10] preserved (not a numeric citation)
result = "Claim about [array index 0:10] performance"
# ✓ Correctly distinguishes citation brackets from content brackets
```

## Testing Results

```
test_bracket_handling.py::TestBracketHandling
  26 tests PASSED ✓

tests/test_nova_factcheck.py
  32 tests PASSED ✓

No regressions detected in existing test suite.
```

## Pattern Documentation for Future Maintainers

The patterns use the following conventions:

1. **Explicit vs Optional Brackets**
   - Explicit: `\[...\]` - must have brackets
   - Optional: `\[?...\]?` - AVOID (causes ambiguity)

2. **Non-Greedy Matching**
   - Use `*?` instead of `*` when matching content between delimiters
   - Pattern: `\[.*?\]` matches from first `[` to first `]`

3. **Pattern Ordering**
   - Most specific patterns first (bracketed forms)
   - General patterns last (unbracketed forms)
   - First match wins in fallback scenario

4. **Whitespace Handling**
   - After citation removal, normalize spaces with `re.sub(r"\s+", " ", text)`
   - Prevents accumulation of multiple spaces

## Files Modified

1. **nova_factcheck/extractor.py**
   - Lines 64-69: Improved inline confidence patterns
   - Lines 179-196: Enhanced `_clean()` method with documentation

2. **test_bracket_handling.py** (NEW)
   - Comprehensive test suite with 26 tests
   - Covers all bracket handling scenarios

## Backward Compatibility

✓ **Fully backward compatible**

The changes only affect:
- Explicit confidence marker extraction (now more robust)
- Whitespace handling (improvement only)
- No API changes
- All existing tests pass

## Future Improvements

Consider adding:
1. Support for custom bracket types (e.g., angle brackets `<>`)
2. Nested bracket validation
3. Performance metrics for large claim batches
4. Configuration options for bracket handling behavior
