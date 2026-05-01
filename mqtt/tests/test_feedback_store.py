"""Comprehensive tests for FeedbackStore with deduplication and Bayesian confidence updates."""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from mqtt.models import Confirmation, normalize_uuid
from mqtt.feedback_store import FeedbackStore, now_iso


class TestFeedbackStore:
    """Test suite for FeedbackStore class."""
    
    @pytest.fixture
    def temp_db(self) -> str:
        """Create a temporary database for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_feedback.db")
            yield db_path
    
    @pytest.fixture
    def store(self, temp_db: str) -> FeedbackStore:
        """Create a FeedbackStore instance for testing."""
        return FeedbackStore(db_path=temp_db)
    
    def test_store_initialization(self, temp_db: str) -> None:
        """Test FeedbackStore initializes database correctly."""
        store = FeedbackStore(db_path=temp_db)
        
        # Check that database file was created
        assert Path(temp_db).exists()
        
        # Check that tables were created
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        
        assert 'findings' in tables
        assert 'feedback_events' in tables
        assert 'orphaned_confirmations' in tables
        conn.close()
    
    def test_store_finding(self, store: FeedbackStore) -> None:
        """Test storing a finding."""
        finding_id = "550e8400e29b41d4a716446655440000"
        
        result = store.store_finding(
            finding_id=finding_id,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.75,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            claim_ids=["claim1", "claim2"],
            anomalies=["anomaly1"],
            severity="high",
            metadata={"key": "value"}
        )
        
        assert result is True
    
    def test_record_confirmation_new_finding(self, store: FeedbackStore) -> None:
        """Test recording a confirmation for a new finding."""
        finding_id = "550e8400e29b41d4a716446655440000"
        
        # Store the finding first
        store.store_finding(
            finding_id=finding_id,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.5,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        )
        
        # Create and record confirmation
        confirmation = Confirmation(
            finding_id=finding_id,
            device_id="device_001",
            confirmed=True,
            evidence="Found duplicate steps",
            timestamp=now_iso()
        )
        
        result = store.record_confirmation(confirmation)
        
        assert result['success'] is True
        assert result['finding_updated'] is True
        assert result['reason'] == 'confirmation_processed'
        assert 'new_confidence' in result
        assert 0.0 <= result['new_confidence'] <= 1.0
    
    def test_deduplication_same_hash(self, store: FeedbackStore) -> None:
        """Test that duplicate confirmations (same hash) are ignored."""
        finding_id = "550e8400e29b41d4a716446655440000"
        
        # Store the finding
        store.store_finding(
            finding_id=finding_id,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.5,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        )
        
        # Create confirmation
        ts = now_iso()
        confirmation = Confirmation(
            finding_id=finding_id,
            device_id="device_001",
            confirmed=True,
            evidence="Found duplicate",
            timestamp=ts
        )
        
        # Record first confirmation
        result1 = store.record_confirmation(confirmation)
        assert result1['success'] is True
        assert result1['finding_updated'] is True
        first_confidence = result1['new_confidence']
        
        # Record same confirmation again (same hash due to bucketed timestamp)
        result2 = store.record_confirmation(confirmation)
        assert result2['success'] is True
        assert result2['finding_updated'] is False
        assert result2['reason'] == 'duplicate_ignored'
    
    def test_bayesian_confidence_confirmed(self, store: FeedbackStore) -> None:
        """Test Bayesian confidence update for confirmed findings."""
        old_conf = 0.5
        new_conf = store._calculate_bayesian_confidence(
            old_confidence=old_conf,
            confirmed=True,
            hours_since_finding=0.0
        )
        
        # Confirmed: new = old + (1.0 - old) * 0.1
        expected = old_conf + (1.0 - old_conf) * 0.1
        assert abs(new_conf - expected) < 0.0001
        assert new_conf > old_conf  # Should increase
    
    def test_bayesian_confidence_rejected(self, store: FeedbackStore) -> None:
        """Test Bayesian confidence update for rejected findings."""
        old_conf = 0.5
        new_conf = store._calculate_bayesian_confidence(
            old_confidence=old_conf,
            confirmed=False,
            hours_since_finding=0.0
        )
        
        # Rejected: new = old * 0.8
        expected = old_conf * 0.8
        assert abs(new_conf - expected) < 0.0001
        assert new_conf < old_conf  # Should decrease
    
    def test_bayesian_confidence_staleness(self, store: FeedbackStore) -> None:
        """Test staleness penalty in Bayesian model."""
        old_conf = 0.8
        hours = 24.0
        
        new_conf = store._calculate_bayesian_confidence(
            old_confidence=old_conf,
            confirmed=True,
            hours_since_finding=hours
        )
        
        # Staleness: 0.8 * (0.95 ** 24) ≈ 0.265
        stale = old_conf * (0.95 ** hours)
        expected = stale + (1.0 - stale) * 0.1
        
        assert abs(new_conf - expected) < 0.0001
        assert new_conf < old_conf  # Even with confirmation, staleness dominates
    
    def test_bayesian_confidence_clamping(self, store: FeedbackStore) -> None:
        """Test that confidence values are clamped to [0.0, 1.0]."""
        # Test lower bound
        new_conf_low = store._calculate_bayesian_confidence(
            old_confidence=0.01,
            confirmed=False,
            hours_since_finding=100.0
        )
        assert new_conf_low >= 0.0
        
        # Test upper bound
        new_conf_high = store._calculate_bayesian_confidence(
            old_confidence=0.99,
            confirmed=True,
            hours_since_finding=100.0
        )
        assert new_conf_high <= 1.0
    
    def test_orphaned_confirmation_finding_not_found(self, store: FeedbackStore) -> None:
        """Test recording confirmation for non-existent finding (orphaned)."""
        finding_id = "550e8400e29b41d4a716446655440000"
        
        # Don't store the finding - create confirmation anyway
        confirmation = Confirmation(
            finding_id=finding_id,
            device_id="device_001",
            confirmed=True,
            evidence="Evidence",
            timestamp=now_iso()
        )
        
        result = store.record_confirmation(confirmation)
        
        assert result['success'] is True
        assert result['finding_updated'] is False
        assert result['reason'] == 'orphaned_finding'
        assert 'error' in result
    
    def test_get_orphaned_confirmations(self, store: FeedbackStore) -> None:
        """Test retrieving orphaned confirmations."""
        # Create two orphaned confirmations
        finding_id1 = "550e8400e29b41d4a716446655440000"
        finding_id2 = "550e8400e29b41d4a716446655440001"
        
        import time
        for fid in [finding_id1, finding_id2]:
            time.sleep(0.01)  # Ensure different timestamp buckets
            confirmation = Confirmation(
                finding_id=fid,
                device_id="device_001",
                confirmed=True,
                evidence="Test",
                timestamp=now_iso()
            )
            store.record_confirmation(confirmation)
        
        orphaned = store.get_orphaned_confirmations()
        
        assert len(orphaned) == 2
        assert all(o['finding_id'] in [finding_id1, finding_id2] for o in orphaned)
        assert all('error_message' in o for o in orphaned)
    
    def test_uuid_normalization_in_lookup(self, store: FeedbackStore) -> None:
        """Test UUID normalization works in lookups (hyphenated vs hex)."""
        # Use hyphenated UUID
        hyphenated = "550e8400-e29b-41d4-a716-446655440000"
        normalized = "550e8400e29b41d4a716446655440000"
        
        # Store with hyphenated
        store.store_finding(
            finding_id=hyphenated,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.5,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        )
        
        # Confirm with normalized (should find the finding)
        confirmation = Confirmation(
            finding_id=normalized,
            device_id="device_001",
            confirmed=True,
            evidence="Test",
            timestamp=now_iso()
        )
        
        result = store.record_confirmation(confirmation)
        assert result['success'] is True
        assert result['finding_updated'] is True
    
    def test_get_finding_stats_global(self, store: FeedbackStore) -> None:
        """Test getting global finding statistics."""
        # Store a finding
        finding_id = "550e8400e29b41d4a716446655440000"
        store.store_finding(
            finding_id=finding_id,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.5,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        )
        
        # Record confirmations with different timestamps to avoid deduplication
        import time
        confirmations = [True, True, False]
        for i, confirmed in enumerate(confirmations):
            time.sleep(0.01)  # Small delay to ensure different timestamp buckets
            confirmation = Confirmation(
                finding_id=finding_id,
                device_id="device_001",
                confirmed=confirmed,
                evidence="Test",
                timestamp=now_iso()
            )
            store.record_confirmation(confirmation)
        
        stats = store.get_finding_stats()
        
        assert stats['total_confirmations'] == 3
        assert stats['confirmed_count'] == 2
        assert stats['rejected_count'] == 1
        assert abs(stats['confirmation_rate'] - (2/3)) < 0.01
        assert stats['unique_devices'] >= 1
        assert stats['unique_findings'] >= 1
    
    def test_get_finding_stats_by_device(self, store: FeedbackStore) -> None:
        """Test getting finding statistics filtered by device."""
        # Store a finding
        finding_id = "550e8400e29b41d4a716446655440000"
        store.store_finding(
            finding_id=finding_id,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.5,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        )
        
        # Record confirmations for specific device
        confirmation = Confirmation(
            finding_id=finding_id,
            device_id="device_001",
            confirmed=True,
            evidence="Test",
            timestamp=now_iso()
        )
        store.record_confirmation(confirmation)
        
        stats = store.get_finding_stats(device_id="device_001")
        
        assert stats['total_confirmations'] == 1
        assert stats['confirmed_count'] == 1
        assert stats['rejected_count'] == 0
        assert stats['confirmation_rate'] == 1.0
    
    def test_cleanup_expired_findings(self, store: FeedbackStore) -> None:
        """Test cleanup of expired findings."""
        # Store an expired finding
        expired_id = "550e8400e29b41d4a716446655440000"
        store.store_finding(
            finding_id=expired_id,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.5,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        )
        
        # Store a valid finding
        valid_id = "550e8400e29b41d4a716446655440001"
        store.store_finding(
            finding_id=valid_id,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.5,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        )
        
        deleted = store.cleanup_expired_findings()
        
        assert deleted >= 1
    
    def test_foreign_key_constraint_violation(self, store: FeedbackStore) -> None:
        """Test foreign key constraint violation is caught and handled."""
        # Try to record confirmation for finding that doesn't exist
        confirmation = Confirmation(
            finding_id="550e8400e29b41d4a716446655440099",
            device_id="device_001",
            confirmed=True,
            evidence="Should fail FK",
            timestamp=now_iso()
        )
        
        result = store.record_confirmation(confirmation)
        
        # Should be stored as orphaned, not crash
        assert result['success'] is True
        assert result['reason'] == 'orphaned_finding'
    
    def test_multiple_devices_same_finding(self, store: FeedbackStore) -> None:
        """Test multiple devices confirming the same finding."""
        finding_id = "550e8400e29b41d4a716446655440000"
        
        # Store finding
        store.store_finding(
            finding_id=finding_id,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.5,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        )
        
        # Multiple devices confirm
        for device_id in ["device_001", "device_002", "device_003"]:
            confirmation = Confirmation(
                finding_id=finding_id,
                device_id=device_id,
                confirmed=True,
                evidence=f"Confirmed by {device_id}",
                timestamp=now_iso()
            )
            store.record_confirmation(confirmation)
        
        stats = store.get_finding_stats()
        assert stats['total_confirmations'] == 3
        assert stats['unique_devices'] >= 3
    
    def test_confirmation_evidence_persisted(self, store: FeedbackStore) -> None:
        """Test that confirmation evidence is persisted."""
        finding_id = "550e8400e29b41d4a716446655440000"
        evidence_text = "Detailed evidence of the anomaly"
        
        # Store finding
        store.store_finding(
            finding_id=finding_id,
            device_id="device_001",
            finding_type="StepDuplication",
            confidence=0.5,
            timestamp=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        )
        
        # Store confirmation with evidence
        confirmation = Confirmation(
            finding_id=finding_id,
            device_id="device_001",
            confirmed=True,
            evidence=evidence_text,
            timestamp=now_iso()
        )
        
        result = store.record_confirmation(confirmation)
        assert result['success'] is True
        
        # Verify orphaned (for checking evidence was stored)
        orphaned = store.get_orphaned_confirmations()
        # Since finding exists, no orphaned records yet
        # But if we create another orphaned one with same evidence...
        import time
        time.sleep(0.01)  # Ensure different timestamp bucket
        fake_finding = "550e8400e29b41d4a716446655440099"
        confirmation2 = Confirmation(
            finding_id=fake_finding,
            device_id="device_001",
            confirmed=True,
            evidence=evidence_text,
            timestamp=now_iso()
        )
        store.record_confirmation(confirmation2)
        
        orphaned = store.get_orphaned_confirmations()
        assert len(orphaned) > 0
        assert evidence_text in str(orphaned[0]['payload'])
    
    def test_close_database(self, store: FeedbackStore) -> None:
        """Test closing the database connection."""
        store.close()
        # Should not raise an error


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
