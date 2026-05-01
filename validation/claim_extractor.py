"""Claim extraction from model outputs.

Extracts atomic claims from large language model reasoning passes.
Parses confidence scores, identifies claim types, and structures them for validation.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from .types import Claim

log = logging.getLogger(__name__)


class ClaimExtractor:
    """Extracts structured claims from model output.
    
    Handles different task classes and extracts claims with confidence scores,
    subjects, and expected values for validation.
    """
    
    def __init__(self, task_class: str = "general"):
        """Initialize claim extractor.
        
        Args:
            task_class: Type of reasoning task ('general', 'code_review', etc.)
                       This affects what types of claims are extractable.
        """
        self.task_class = task_class
    
    def extract_claims(
        self,
        output: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Claim]:
        """Extract claims from model output.
        
        Splits output into sentences, filters short/question sentences,
        extracts confidence scores and claim types, and constructs Claim objects.
        
        Args:
            output: Model output text to extract claims from
            context: Optional context (may include task_class, prior_claims, etc.)
        
        Returns:
            List of extracted Claim objects
        """
        claims = []
        context = context or {}
        
        # Use output text as-is
        if not output or not output.strip():
            log.debug("Empty output, returning no claims")
            return claims
        
        # Pre-clean: remove "CLAIM:" prefixes
        output = re.sub(r'\bCLAIM:\s*', '', output, flags=re.IGNORECASE)
        
        # Extract all sentences with their associated confidence markers
        sentences_with_confidence = []
        lines = output.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Split line by sentence boundaries
            raw_sentences = re.split(r'(?<=[.!?])\s+', line)
            
            # Process sentences and merge confidence markers
            for i, sentence in enumerate(raw_sentences):
                sentence = sentence.strip()
                if not sentence:
                    continue
                
                # Check if this is a confidence marker (standalone or in brackets)
                conf_match = re.match(r'^\[?\s*[Cc]onfidence[:\s]+(\d+)%?\s*\]?$', sentence)
                if conf_match:
                    # This is a standalone confidence marker - associate with previous sentence
                    if sentences_with_confidence:
                        prev_sentence, _ = sentences_with_confidence.pop()
                        conf_val = float(conf_match.group(1))
                        if conf_val > 1:
                            conf_val = conf_val / 100.0
                        confidence_model = min(1.0, max(0.0, conf_val))
                        sentences_with_confidence.append((prev_sentence, confidence_model))
                    continue
                
                # Not a confidence marker - process as regular sentence
                # Filter short sentences
                if len(sentence) < 10:
                    continue
                
                # Skip questions and commands
                if sentence.endswith('?') or \
                   sentence.startswith(('What', 'How', 'When', 'Where', 'Why', 'Do', 'Can', 'Should', 'Is', 'This')):
                    continue
                
                # Extract confidence from sentence
                confidence_model = 0.5
                confidence_patterns = [
                    r'\[?\s*[Cc]onfidence[:\s]+(\d+)%?\s*\]?',
                    r'\[?\s*(\d+)%?\s+confidence\s*\]?',
                ]
                for pattern in confidence_patterns:
                    confidence_match = re.search(pattern, sentence)
                    if confidence_match:
                        try:
                            conf_val = float(confidence_match.group(1))
                            if conf_val > 1:
                                conf_val = conf_val / 100.0
                            confidence_model = min(1.0, max(0.0, conf_val))
                            sentence = re.sub(pattern, '', sentence, flags=re.IGNORECASE).strip()
                            break
                        except (ValueError, IndexError):
                            pass
                
                if sentence:  # Only add if there's content left
                    sentences_with_confidence.append((sentence, confidence_model))
        
        # Second pass: create claims
        seen = set()
        claim_id_counter = 0
        pass_num = context.get("pass_num", 0)
        
        for sentence, confidence_model in sentences_with_confidence:
            # Skip duplicate sentences
            if sentence in seen:
                continue
            seen.add(sentence)
            
            # Infer claim type based on keywords
            # Note: Order matters! More specific patterns should be checked first
            claim_type = "general"
            sentence_lower = sentence.lower()
            
            # Check staleness first (takes precedence over GPS)
            if any(x in sentence_lower for x in ['stale', 'fresh', 'age', 'staleness', 'updated', 'outdated']):
                claim_type = "telemetry_staleness"
            elif any(x in sentence_lower for x in ['gps', 'position', 'location', 'latitude', 'longitude']):
                claim_type = "telemetry_gps"
            elif any(x in sentence_lower for x in ['error', 'failure', 'failed', 'crash', 'exception']):
                claim_type = "error_detection"
            elif any(x in sentence_lower for x in ['code', 'bug', 'defect', 'vulnerability', 'issue']):
                claim_type = "code_defect"
            elif any(x in sentence_lower for x in ['database', 'connection', 'network', 'timeout', 'latency']):
                claim_type = "system_health"
            elif any(x in sentence_lower for x in ['hallucination', 'contradiction', 'inconsistent']):
                claim_type = "consistency"
            elif any(x in sentence_lower for x in ['battery', 'memory', 'cpu', 'temperature', 'sensor']):
                claim_type = "device_metric"
            
            # Extract subject (first meaningful word/phrase)
            subject = "unknown"
            words = [w.rstrip('.,;:') for w in sentence.split() if len(w) > 1]
            if len(words) > 0:
                for word in words:
                    if len(word) > 2 and (word[0].isupper() or word in ['GPS', 'CPU', 'RAM', 'API', 'USB']):
                        subject = word
                        break
                if subject == "unknown" and len(words) > 0:
                    subject = words[0]
            
            # Generate claim ID
            claim_id = f"claim_{pass_num}_{claim_id_counter}"
            claim_id_counter += 1
            
            try:
                claim = Claim(
                    id=claim_id,
                    statement=sentence.strip(),
                    claim_type=claim_type,
                    subject=subject,
                    expected_value={"inferred": True, "task_class": self.task_class},
                    confidence_model=confidence_model,
                )
                claims.append(claim)
            except (TypeError, KeyError) as e:
                log.debug(f"Failed to create claim: {e}")
                continue
        
        log.debug(f"Extracted {len(claims)} claims from output")
        return claims


def extract_claims_from_pass_output(
    pass_output: str,
    pass_num: int = 0,
    task_class: str = "general",
) -> List[Claim]:
    """Extract claims from a single reasoning pass output.
    
    Args:
        pass_output: Raw text output from a reasoning pass
        pass_num: Pass number (for ID generation)
        task_class: Type of task (affects extraction logic)
    
    Returns:
        List of extracted Claim objects
    """
    extractor = ClaimExtractor(task_class=task_class)
    context = {"pass_num": pass_num}
    return extractor.extract_claims(pass_output, context=context)
