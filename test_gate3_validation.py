#!/usr/bin/env python3
"""
GATE 3 VALIDATION: Real System Integration
Tests the end-to-end validation pipeline with real Great Library data
"""

import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine import deep_think_passes
from ground_truth import NovaGroundTruthProvider, Claim


async def test_gate3_validation():
    """Test complete validation pipeline with real data"""
    
    print("\n" + "="*80)
    print("GATE 3 VALIDATION: Real System Integration")
    print("="*80)
    
    # Initialize Nova provider
    provider = NovaGroundTruthProvider()
    
    print("\n[1] Testing data_governance task class with validation")
    print("-" * 80)
    
    try:
        # Run a data_governance reasoning pass with validation
        result = await deep_think_passes(
            question="What are the characteristics of DAMA phone sensor data quality?",
            task_class="data_governance",
            passes=1,
            ground_truth_provider=provider,
        )
        
        print(f"✓ data_governance pass completed")
        print(f"  - Passes collected: {len(result.get('pass_history', []))}")
        
        # Check validation results
        if result.get('pass_history'):
            last_pass = result['pass_history'][-1]
            validation = last_pass.get('validation', {})
            measured_conf = last_pass.get('measured_confidence', 0)
            
            print(f"  - Validation result: {validation.get('status', 'unknown')}")
            print(f"  - Measured confidence: {measured_conf:.2f}")
            print(f"  - Hallucination count: {validation.get('hallucination_count', 0)}")
            
            # PASS if: measured_confidence > 0.6 and hallucination_count < 3
            if measured_conf > 0.6 and validation.get('hallucination_count', 0) < 3:
                print("  ✅ PASS: data_governance validation")
            else:
                print(f"  ⚠️  WARN: data_governance validation below threshold")
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n[2] Testing research_synthesis task class with validation")
    print("-" * 80)
    
    try:
        # Run a research_synthesis reasoning pass with validation
        result = await deep_think_passes(
            question="What does sensor fusion research say about combining GPS and WiFi positioning?",
            task_class="research_synthesis",
            passes=1,
            ground_truth_provider=provider,
        )
        
        print(f"✓ research_synthesis pass completed")
        print(f"  - Passes collected: {len(result.get('pass_history', []))}")
        
        # Check validation results
        if result.get('pass_history'):
            last_pass = result['pass_history'][-1]
            validation = last_pass.get('validation', {})
            measured_conf = last_pass.get('measured_confidence', 0)
            
            print(f"  - Validation result: {validation.get('status', 'unknown')}")
            print(f"  - Measured confidence: {measured_conf:.2f}")
            print(f"  - Hallucination count: {validation.get('hallucination_count', 0)}")
            
            # PASS if: all claims have confidence > 0.7 (research should be well-grounded)
            if measured_conf > 0.7:
                print("  ✅ PASS: research_synthesis validation")
            else:
                print(f"  ⚠️  WARN: research_synthesis validation below threshold")
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "="*80)
    print("GATE 3 VALIDATION: Summary")
    print("="*80)
    print("\nIf both tests show ✅ PASS, Gate 3 is CLEARED")
    print("Ready to proceed to Phase 4: Pre-Restart Sanity Check")
    print("\n")


if __name__ == "__main__":
    asyncio.run(test_gate3_validation())
