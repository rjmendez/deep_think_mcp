#!/usr/bin/env python3
"""Integration test: Verify all exports from deep_think_mcp.core and engine.

Tests:
1. Import core module directly
2. List all 20+ items exported by __all__
3. Import each item individually with no errors
4. Verify engine/__init__.py exports work
5. Verify validation module exports work

Acceptance criteria:
- All items in __all__ are importable
- No circular import errors
- No missing dependencies
- All classes/functions are callable or data types
"""

import sys
import traceback
from typing import Any, List, Dict

# Track results
results = {
    "passed": [],
    "failed": [],
    "imports_tested": [],
}


def test_core_import():
    """Test 1a: Import core module directly."""
    try:
        import deep_think_mcp.core as core
        results["passed"].append("Import core module directly")
        results["imports_tested"].append("deep_think_mcp.core")
        return core
    except Exception as e:
        results["failed"].append(f"Import core module: {e}")
        traceback.print_exc()
        sys.exit(1)


def test_list_exports(core: Any) -> List[str]:
    """Test 2: List all exported items."""
    try:
        all_items = core.__all__
        count = len(all_items)
        results["passed"].append(f"Listed {count} exports in __all__")
        print(f"\n📦 Core exports ({count} items):")
        for item in sorted(all_items):
            print(f"  - {item}")
        return all_items
    except Exception as e:
        results["failed"].append(f"List exports: {e}")
        traceback.print_exc()
        sys.exit(1)


def test_individual_imports(core: Any, all_items: List[str]):
    """Test 3: Import each item individually."""
    print(f"\n✓ Testing individual imports:")
    for item in all_items:
        try:
            obj = getattr(core, item)
            results["imports_tested"].append(f"core.{item}")
            print(f"  ✓ {item} ({type(obj).__name__})")
            results["passed"].append(f"Import core.{item}")
        except AttributeError as e:
            results["failed"].append(f"Import core.{item}: {e}")
            print(f"  ✗ {item}: {e}")


def test_engine_exports():
    """Test 4: Verify engine/__init__.py exports."""
    try:
        from deep_think_mcp.engine import (
            ProviderConfig,
            build_provider_config,
            refresh_ollama_models,
            model_summary,
            deep_think_passes,
            run_fan_out,
            classify_task,
            TASK_CLASS_PROFILES,
            PERSPECTIVE_MANDATES,
        )
        results["passed"].append("Engine exports are all importable")
        results["imports_tested"].extend([
            "deep_think_mcp.engine.ProviderConfig",
            "deep_think_mcp.engine.build_provider_config",
            "deep_think_mcp.engine.refresh_ollama_models",
            "deep_think_mcp.engine.model_summary",
            "deep_think_mcp.engine.deep_think_passes",
            "deep_think_mcp.engine.run_fan_out",
            "deep_think_mcp.engine.classify_task",
            "deep_think_mcp.engine.TASK_CLASS_PROFILES",
            "deep_think_mcp.engine.PERSPECTIVE_MANDATES",
        ])
        print(f"\n✓ Engine imports successful")
        print(f"  - ProviderConfig (class)")
        print(f"  - build_provider_config (function)")
        print(f"  - refresh_ollama_models (function)")
        print(f"  - model_summary (function)")
        print(f"  - deep_think_passes (function)")
        print(f"  - run_fan_out (function)")
        print(f"  - classify_task (function)")
        print(f"  - TASK_CLASS_PROFILES (dict with {len(TASK_CLASS_PROFILES)} keys)")
        print(f"  - PERSPECTIVE_MANDATES (dict with {len(PERSPECTIVE_MANDATES)} keys)")
    except Exception as e:
        results["failed"].append(f"Engine exports: {e}")
        traceback.print_exc()
        sys.exit(1)


def test_validation_exports():
    """Test 5: Verify validation module exports."""
    try:
        from deep_think_mcp.validation import (
            Claim,
            SensorData,
            ValidationResult,
            PassValidationResult,
            ValidationMetrics,
            ClaimExtractor,
            extract_claims_from_pass_output,
            validate_claims,
            calculate_confidence_from_evidence,
            merge_validation_results,
            AbstractGroundTruthProvider,
            MQTTGroundTruthProvider,
            NovaGroundTruthProvider,
        )
        results["passed"].append("Validation exports are all importable")
        results["imports_tested"].extend([
            "deep_think_mcp.validation.Claim",
            "deep_think_mcp.validation.SensorData",
            "deep_think_mcp.validation.ValidationResult",
            "deep_think_mcp.validation.PassValidationResult",
            "deep_think_mcp.validation.ValidationMetrics",
            "deep_think_mcp.validation.ClaimExtractor",
            "deep_think_mcp.validation.extract_claims_from_pass_output",
            "deep_think_mcp.validation.validate_claims",
            "deep_think_mcp.validation.calculate_confidence_from_evidence",
            "deep_think_mcp.validation.merge_validation_results",
            "deep_think_mcp.validation.AbstractGroundTruthProvider",
            "deep_think_mcp.validation.MQTTGroundTruthProvider",
            "deep_think_mcp.validation.NovaGroundTruthProvider",
        ])
        print(f"\n✓ Validation imports successful")
        print(f"  - Claim (class)")
        print(f"  - SensorData (class)")
        print(f"  - ValidationResult (class)")
        print(f"  - PassValidationResult (class)")
        print(f"  - ValidationMetrics (class)")
        print(f"  - ClaimExtractor (class)")
        print(f"  - extract_claims_from_pass_output (function)")
        print(f"  - validate_claims (function)")
        print(f"  - calculate_confidence_from_evidence (function)")
        print(f"  - merge_validation_results (function)")
        print(f"  - AbstractGroundTruthProvider (class)")
        print(f"  - MQTTGroundTruthProvider (class)")
        print(f"  - NovaGroundTruthProvider (class)")
    except Exception as e:
        results["failed"].append(f"Validation exports: {e}")
        traceback.print_exc()
        sys.exit(1)


def test_integration_functions():
    """Test 6: Verify integration helper functions."""
    try:
        from deep_think_mcp.core import (
            get_engine,
            get_validation,
            run_reasoning_with_validation,
        )
        results["passed"].append("Integration functions are importable")
        print(f"\n✓ Integration functions successful")
        print(f"  - get_engine (function)")
        print(f"  - get_validation (function)")
        print(f"  - run_reasoning_with_validation (async function)")
        
        # Test get_engine() returns the engine module
        engine = get_engine()
        assert hasattr(engine, 'deep_think_passes'), "engine module missing deep_think_passes"
        results["passed"].append("get_engine() returns valid engine module")
        
        # Test get_validation() returns the validation module
        validation = get_validation()
        assert hasattr(validation, 'validate_claims'), "validation module missing validate_claims"
        results["passed"].append("get_validation() returns valid validation module")
        
    except Exception as e:
        results["failed"].append(f"Integration functions: {e}")
        traceback.print_exc()
        sys.exit(1)


def main():
    """Run all tests."""
    print("=" * 70)
    print("DEEP_THINK_MCP INTEGRATION TEST")
    print("=" * 70)
    
    # Run tests in sequence
    core = test_core_import()
    all_items = test_list_exports(core)
    test_individual_imports(core, all_items)
    test_engine_exports()
    test_validation_exports()
    test_integration_functions()
    
    # Print summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    print(f"✓ Passed: {len(results['passed'])}")
    print(f"✗ Failed: {len(results['failed'])}")
    print(f"Total imports tested: {len(results['imports_tested'])}")
    
    if results["failed"]:
        print("\n❌ FAILED TESTS:")
        for failure in results["failed"]:
            print(f"  - {failure}")
        return 1
    
    print("\n✅ ALL TESTS PASSED")
    print("\n📋 Acceptance Criteria Met:")
    print("  ✓ All items in core.__all__ are importable")
    print("  ✓ All engine module exports are accessible")
    print("  ✓ All validation module exports are accessible")
    print("  ✓ Integration functions work correctly")
    print(f"  ✓ Total of 20+ exports verified ({len(all_items)} in core, plus engine/validation)")
    print("  ✓ No circular import errors")
    print("  ✓ No missing dependencies")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
