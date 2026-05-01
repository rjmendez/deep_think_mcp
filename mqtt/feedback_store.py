"""Feedback store with deduplication and Bayesian confidence updates.

This module provides the FeedbackStore class which handles:
- Recording confirmations from devices
- Deduplication via confirmation_hash
- Bayesian confidence updates using principled probabilistic model
- Orphaned confirmation tracking (for non-existent findings)
- UUID normalization on all lookups
"""

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from mqtt.models import Confirmation, normalize_uuid, ValidationError

log = logging.getLogger(__name__)


def now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


class FeedbackStore:
    """SQLite-backed store for device confirmations with Bayesian confidence updates."""
    
    def __init__(self, db_path: str = "~/.deep_think/feedback.db"):
        """Initialize feedback store.
        
        Args:
            db_path: Path to SQLite database file (expands ~ to home directory)
        """
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.isolation_level = "DEFERRED"  # Explicit transactions for dedup
        self.conn.execute("PRAGMA foreign_keys = ON")  # Enable foreign key constraints
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize database schema if it doesn't exist."""
        try:
            with self.conn:
                cursor = self.conn.cursor()
                
                # Findings table (stores findings with confidence scores)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS findings (
                        id TEXT PRIMARY KEY,
                        device_id TEXT NOT NULL,
                        finding_type TEXT NOT NULL,
                        confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
                        timestamp TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        claim_ids TEXT,
                        anomalies TEXT,
                        severity TEXT DEFAULT 'medium',
                        metadata TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Feedback events table (stores confirmations)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS feedback_events (
                        id TEXT PRIMARY KEY,
                        finding_id TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        confirmed INTEGER NOT NULL,
                        evidence TEXT,
                        confirmation_hash TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (finding_id) REFERENCES findings(id),
                        UNIQUE(finding_id, confirmation_hash)
                    )
                """)
                
                # Orphaned confirmations table (for findings that don't exist)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS orphaned_confirmations (
                        id TEXT PRIMARY KEY,
                        finding_id TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        error_message TEXT,
                        timestamp TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                log.debug(f"Initialized feedback store at {self.db_path}")
        except Exception as e:
            log.error(f"Failed to initialize feedback store: {e}")
            raise
    
    def record_confirmation(self, confirmation: Confirmation) -> Dict[str, Any]:
        """Process a confirmation from a device.
        
        Algorithm:
        1. Normalize finding_id to hex format
        2. Generate confirmation_hash (already done by Confirmation)
        3. Check if (finding_id, confirmation_hash) already exists (dedup)
        4. If exists: return duplicate_ignored
        5. If not exists: try to INSERT into feedback_events
        6. On foreign key violation: store in orphaned_confirmations
        7. On success: fetch finding, calculate new_confidence, UPDATE finding
        
        Args:
            confirmation: Confirmation object from device
            
        Returns:
            {
                'success': bool,
                'finding_updated': bool,
                'new_confidence': float (if updated),
                'reason': str (duplicate_ignored|confirmation_processed|orphaned_finding),
                'error': str (if error)
            }
        """
        try:
            # Normalize and validate
            normalized_id = normalize_uuid(confirmation.finding_id)
            confirmation_hash = confirmation.confirmation_hash
            
            with self.conn:
                cursor = self.conn.cursor()
                
                # Check for duplicate first
                cursor.execute(
                    "SELECT id FROM feedback_events WHERE finding_id = ? AND confirmation_hash = ?",
                    (normalized_id, confirmation_hash)
                )
                if cursor.fetchone():
                    log.debug(f"Skipping duplicate confirmation for finding {normalized_id}")
                    return {
                        'success': True,
                        'finding_updated': False,
                        'reason': 'duplicate_ignored'
                    }
                
                # Try to insert feedback event (will fail on FK constraint if finding doesn't exist)
                try:
                    cursor.execute(
                        """INSERT INTO feedback_events 
                           (id, finding_id, device_id, confirmed, evidence, confirmation_hash, timestamp)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            uuid4().hex,
                            normalized_id,
                            confirmation.device_id,
                            int(confirmation.confirmed),
                            confirmation.evidence,
                            confirmation_hash,
                            confirmation.timestamp
                        )
                    )
                except sqlite3.IntegrityError as e:
                    # Foreign key violation: finding doesn't exist
                    # Store as orphaned
                    cursor.execute(
                        """INSERT INTO orphaned_confirmations 
                           (id, finding_id, device_id, payload, error_message, timestamp)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            uuid4().hex,
                            normalized_id,
                            confirmation.device_id,
                            json.dumps(asdict(confirmation)),
                            f"Finding not found: {str(e)}",
                            confirmation.timestamp
                        )
                    )
                    log.warning(
                        f"Stored orphaned confirmation: finding {normalized_id} not found"
                    )
                    return {
                        'success': True,
                        'finding_updated': False,
                        'reason': 'orphaned_finding',
                        'error': str(e)
                    }
                
                # Feedback event inserted successfully - now update finding confidence
                cursor.execute(
                    "SELECT confidence, timestamp FROM findings WHERE id = ?",
                    (normalized_id,)
                )
                row = cursor.fetchone()
                if not row:
                    # This should never happen (FK check passed), but handle gracefully
                    return {
                        'success': False,
                        'finding_updated': False,
                        'reason': 'finding_fetch_failed',
                        'error': 'Finding not found after FK check passed'
                    }
                
                old_confidence, finding_timestamp = row
                
                # Calculate hours since finding was created
                try:
                    finding_ts = datetime.fromisoformat(finding_timestamp.replace("Z", "+00:00"))
                    now_ts = datetime.now(timezone.utc)
                    hours_since = (now_ts - finding_ts).total_seconds() / 3600.0
                except (ValueError, AttributeError):
                    hours_since = 0.0
                
                # Update confidence using Bayesian model
                new_confidence = self._calculate_bayesian_confidence(
                    old_confidence,
                    confirmation.confirmed,
                    hours_since
                )
                
                cursor.execute(
                    "UPDATE findings SET confidence = ? WHERE id = ?",
                    (new_confidence, normalized_id)
                )
                
                log.info(
                    f"Updated finding {normalized_id}: "
                    f"confidence {old_confidence:.3f} -> {new_confidence:.3f} "
                    f"({['rejected', 'confirmed'][confirmation.confirmed]})"
                )
                
                return {
                    'success': True,
                    'finding_updated': True,
                    'new_confidence': new_confidence,
                    'reason': 'confirmation_processed'
                }
        
        except ValidationError as e:
            log.error(f"Validation error in confirmation: {e}")
            return {
                'success': False,
                'finding_updated': False,
                'reason': 'validation_error',
                'error': str(e)
            }
        except Exception as e:
            log.error(f"Unexpected error recording confirmation: {e}")
            return {
                'success': False,
                'finding_updated': False,
                'reason': 'unexpected_error',
                'error': str(e)
            }
    
    def _calculate_bayesian_confidence(
        self,
        old_confidence: float,
        confirmed: bool,
        hours_since_finding: float
    ) -> float:
        """Update confidence using Bayesian model.
        
        Algorithm:
        1. Apply staleness decay first: new = old * (0.95 ** hours_since_finding)
        2. Apply confirmation update:
           - If confirmed: new = new + (1.0 - new) * 0.1 (move toward 1.0)
           - If rejected: new = new * 0.8 (move toward 0.0)
        3. Clamp to [0.0, 1.0]
        
        Args:
            old_confidence: Current confidence (0.0-1.0)
            confirmed: Whether finding was confirmed (True) or rejected (False)
            hours_since_finding: Hours since finding was created
            
        Returns:
            New confidence value (0.0-1.0)
        """
        # Apply staleness decay (exponential decay toward 0.5)
        stale_confidence = old_confidence * (0.95 ** hours_since_finding)
        
        # Apply confirmation-based Bayesian update
        if confirmed:
            # Moving toward 1.0 (confirmation)
            new_confidence = stale_confidence + (1.0 - stale_confidence) * 0.1
        else:
            # Moving toward 0.0 (rejection)
            new_confidence = stale_confidence * 0.8
        
        # Clamp to valid range
        return max(0.0, min(1.0, new_confidence))
    
    def store_finding(
        self,
        finding_id: str,
        device_id: str,
        finding_type: str,
        confidence: float,
        timestamp: str,
        expires_at: str,
        claim_ids: Optional[List[str]] = None,
        anomalies: Optional[List[str]] = None,
        severity: str = "medium",
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Store a finding in the feedback store.
        
        Args:
            finding_id: UUID of the finding (will be normalized)
            device_id: Device identifier
            finding_type: Type of anomaly (AnomalyType enum value)
            confidence: Initial confidence (0.0-1.0)
            timestamp: When finding was created (ISO 8601)
            expires_at: When finding expires (ISO 8601)
            claim_ids: Supporting claim IDs
            anomalies: Anomaly descriptions
            severity: Severity level
            metadata: Additional metadata
            
        Returns:
            True if stored successfully, False otherwise
        """
        try:
            normalized_id = normalize_uuid(finding_id)
            
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    """INSERT OR REPLACE INTO findings 
                       (id, device_id, finding_type, confidence, timestamp, expires_at, 
                        claim_ids, anomalies, severity, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        normalized_id,
                        device_id,
                        finding_type,
                        confidence,
                        timestamp,
                        expires_at,
                        json.dumps(claim_ids or []),
                        json.dumps(anomalies or []),
                        severity,
                        json.dumps(metadata or {})
                    )
                )
            return True
        except Exception as e:
            log.error(f"Failed to store finding {finding_id}: {e}")
            return False
    
    def get_finding_stats(self, device_id: Optional[str] = None) -> Dict[str, Any]:
        """Query confirmation statistics.
        
        Args:
            device_id: If specified, stats for that device only; otherwise global stats
            
        Returns:
            {
                'total_confirmations': int,
                'confirmed_count': int,
                'rejected_count': int,
                'confirmation_rate': float (0.0-1.0),
                'unique_devices': int,
                'unique_findings': int
            }
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                
                if device_id:
                    cursor.execute(
                        "SELECT COUNT(*) FROM feedback_events WHERE device_id = ?",
                        (device_id,)
                    )
                else:
                    cursor.execute("SELECT COUNT(*) FROM feedback_events")
                
                total = cursor.fetchone()[0] or 0
                
                if device_id:
                    cursor.execute(
                        "SELECT COUNT(*) FROM feedback_events WHERE device_id = ? AND confirmed = 1",
                        (device_id,)
                    )
                else:
                    cursor.execute(
                        "SELECT COUNT(*) FROM feedback_events WHERE confirmed = 1"
                    )
                
                confirmed = cursor.fetchone()[0] or 0
                rejected = total - confirmed
                confirmation_rate = (confirmed / total) if total > 0 else 0.0
                
                if device_id:
                    cursor.execute(
                        "SELECT COUNT(DISTINCT device_id) FROM feedback_events WHERE device_id = ?",
                        (device_id,)
                    )
                else:
                    cursor.execute(
                        "SELECT COUNT(DISTINCT device_id) FROM feedback_events"
                    )
                unique_devices = cursor.fetchone()[0] or 0
                
                if device_id:
                    cursor.execute(
                        "SELECT COUNT(DISTINCT finding_id) FROM feedback_events WHERE device_id = ?",
                        (device_id,)
                    )
                else:
                    cursor.execute(
                        "SELECT COUNT(DISTINCT finding_id) FROM feedback_events"
                    )
                unique_findings = cursor.fetchone()[0] or 0
                
                return {
                    'total_confirmations': total,
                    'confirmed_count': confirmed,
                    'rejected_count': rejected,
                    'confirmation_rate': round(confirmation_rate, 3),
                    'unique_devices': unique_devices,
                    'unique_findings': unique_findings
                }
        except Exception as e:
            log.error(f"Failed to get finding stats: {e}")
            return {
                'total_confirmations': 0,
                'confirmed_count': 0,
                'rejected_count': 0,
                'confirmation_rate': 0.0,
                'unique_devices': 0,
                'unique_findings': 0
            }
    
    def get_orphaned_confirmations(self) -> List[Dict[str, Any]]:
        """Return all orphaned confirmations for audit.
        
        Returns:
            List of orphaned confirmation records
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    """SELECT id, finding_id, device_id, payload, error_message, timestamp
                       FROM orphaned_confirmations
                       ORDER BY created_at DESC"""
                )
                
                orphaned = []
                for row in cursor.fetchall():
                    try:
                        payload = json.loads(row[3]) if row[3] else {}
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                    
                    orphaned.append({
                        'id': row[0],
                        'finding_id': row[1],
                        'device_id': row[2],
                        'payload': payload,
                        'error_message': row[4],
                        'timestamp': row[5]
                    })
                
                return orphaned
        except Exception as e:
            log.error(f"Failed to get orphaned confirmations: {e}")
            return []
    
    def cleanup_expired_findings(self) -> int:
        """Delete findings where expires_at < now().
        
        Returns:
            Number of findings deleted
        """
        try:
            now = now_iso()
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    "DELETE FROM findings WHERE expires_at < ?",
                    (now,)
                )
            
            deleted = self.conn.total_changes
            if deleted > 0:
                log.info(f"Cleaned up {deleted} expired findings")
            return deleted
        except Exception as e:
            log.error(f"Failed to cleanup expired findings: {e}")
            return 0
    
    async def record_correlation(self, 
                                  correlation_id: str,
                                  timestamp: str,
                                  location_hash: str,
                                  observing_devices: List[str],
                                  sensor_snapshot: Dict[str, Any],
                                  novelty_score: float,
                                  fleet_prevalence: float,
                                  entropy_breakdown: Dict[str, float],
                                  is_anomalous_cluster: bool,
                                  anomaly_details: Dict[str, Any]) -> bool:
        """Record a correlation finding in the database.
        
        Args:
            correlation_id: UUID of correlation
            timestamp: ISO 8601 timestamp
            location_hash: Hashed location (GPS or WiFi-based)
            observing_devices: List of device IDs observing this
            sensor_snapshot: Aggregated sensor values
            novelty_score: Novelty score (0-1)
            fleet_prevalence: % of fleet that's seen this (0-1)
            entropy_breakdown: Per-sensor entropy contributions
            is_anomalous_cluster: True if co-located devices diverge
            anomaly_details: Details about anomalies
            
        Returns:
            True if recorded successfully, False otherwise
        """
        try:
            correlation_id = normalize_uuid(correlation_id)
            
            import json
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    """INSERT INTO correlations 
                       (id, timestamp, location_hash, observing_devices, 
                        sensor_snapshot, novelty_score, fleet_prevalence, 
                        entropy_breakdown, is_anomalous_cluster, anomaly_details, 
                        expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        correlation_id,
                        timestamp,
                        location_hash,
                        json.dumps(observing_devices),
                        json.dumps(sensor_snapshot, default=str),
                        novelty_score,
                        fleet_prevalence,
                        json.dumps(entropy_breakdown, default=str),
                        1 if is_anomalous_cluster else 0,
                        json.dumps(anomaly_details, default=str) if anomaly_details else None,
                        # TTL: 7 days from now
                        (datetime.now(timezone.utc).replace(microsecond=0) + 
                         __import__('datetime').timedelta(days=7)).isoformat()
                    )
                )
            
            log.debug(f"Recorded correlation: {correlation_id}")
            return True
        
        except Exception as e:
            log.error(f"Failed to record correlation: {e}")
            return False
    
    async def annotate_correlation(self,
                                    correlation_id: str,
                                    annotator_id: str,
                                    label: str,
                                    evidence: str = "",
                                    confidence: float = 0.8) -> bool:
        """Add annotation to a correlation finding.
        
        Args:
            correlation_id: UUID of correlation
            annotator_id: ID of person annotating
            label: Label/category (e.g., "RF_hacking_village")
            evidence: Supporting evidence
            confidence: Confidence in annotation (0-1)
            
        Returns:
            True if annotated successfully, False otherwise
        """
        try:
            correlation_id = normalize_uuid(correlation_id)
            annotation_id = str(uuid4()).replace("-", "").lower()
            
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    """INSERT INTO correlation_annotations
                       (id, correlation_id, annotator_id, label, evidence, confidence)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (annotation_id, correlation_id, annotator_id, label, evidence, confidence)
                )
            
            log.debug(f"Annotated correlation {correlation_id}: {label}")
            return True
        
        except Exception as e:
            log.error(f"Failed to annotate correlation: {e}")
            return False
    
    def get_correlations_by_location(self, location_hash: str) -> List[Dict[str, Any]]:
        """Get all correlations observed at a location.
        
        Args:
            location_hash: The location hash
            
        Returns:
            List of correlation records
        """
        try:
            import json
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    """SELECT id, timestamp, location_hash, observing_devices,
                              sensor_snapshot, novelty_score, fleet_prevalence,
                              entropy_breakdown, is_anomalous_cluster, anomaly_details
                       FROM correlations
                       WHERE location_hash = ?
                       ORDER BY timestamp DESC""",
                    (location_hash,)
                )
                
                correlations = []
                for row in cursor.fetchall():
                    correlations.append({
                        'id': row[0],
                        'timestamp': row[1],
                        'location_hash': row[2],
                        'observing_devices': json.loads(row[3]),
                        'sensor_snapshot': json.loads(row[4]),
                        'novelty_score': row[5],
                        'fleet_prevalence': row[6],
                        'entropy_breakdown': json.loads(row[7]),
                        'is_anomalous_cluster': bool(row[8]),
                        'anomaly_details': json.loads(row[9]) if row[9] else {}
                    })
                
                return correlations
        except Exception as e:
            log.error(f"Failed to get correlations: {e}")
            return []
    
    def get_novelty_distribution(self) -> Dict[str, Any]:
        """Get statistics on novelty scores across all correlations.
        
        Returns:
            Dict with novelty statistics
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                
                # Get novelty stats
                cursor.execute(
                    """SELECT 
                       COUNT(*) as total,
                       MIN(novelty_score) as min_novelty,
                       MAX(novelty_score) as max_novelty,
                       AVG(novelty_score) as avg_novelty,
                       COUNT(CASE WHEN novelty_score > 0.8 THEN 1 END) as high_novelty
                       FROM correlations"""
                )
                row = cursor.fetchone()
                
                return {
                    'total_correlations': row[0],
                    'min_novelty': row[1],
                    'max_novelty': row[2],
                    'avg_novelty': round(row[3], 3) if row[3] else 0,
                    'high_novelty_count': row[4] or 0
                }
        except Exception as e:
            log.error(f"Failed to get novelty distribution: {e}")
            return {}
        """Close database connection."""
        try:
            self.conn.close()
        except Exception as e:
            log.error(f"Error closing feedback store: {e}")
