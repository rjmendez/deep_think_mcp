"""Tests for MQTT database schema and initialization."""

import pytest
import sqlite3
import tempfile
from pathlib import Path
from mqtt.db_init import init_db
from mqtt.models import Finding, Confirmation, AnomalyType
from datetime import datetime, timezone


@pytest.fixture
def temp_db() -> Path:
    """Provide a temporary database file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    # Cleanup
    db_path.unlink(missing_ok=True)


class TestDatabaseInitialization:
    """Tests for init_db() function."""

    def test_init_db_creates_database_file(self) -> None:
        """Test that init_db creates a database file."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as f:
            db_path = f.name
        # File is now deleted
        assert not Path(db_path).exists()
        init_db(db_path)
        assert Path(db_path).exists()
        # Cleanup
        Path(db_path).unlink(missing_ok=True)

    def test_init_db_creates_findings_table(self, temp_db: Path) -> None:
        """Test that init_db creates the findings table."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        # Query sqlite_master to check if table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='findings'"
        )
        result = cursor.fetchone()
        conn.close()
        
        assert result is not None
        assert result[0] == "findings"

    def test_init_db_creates_feedback_events_table(self, temp_db: Path) -> None:
        """Test that init_db creates the feedback_events table."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='feedback_events'"
        )
        result = cursor.fetchone()
        conn.close()
        
        assert result is not None
        assert result[0] == "feedback_events"

    def test_init_db_creates_orphaned_confirmations_table(self, temp_db: Path) -> None:
        """Test that init_db creates the orphaned_confirmations table."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='orphaned_confirmations'"
        )
        result = cursor.fetchone()
        conn.close()
        
        assert result is not None
        assert result[0] == "orphaned_confirmations"

    def test_init_db_creates_indexes(self, temp_db: Path) -> None:
        """Test that init_db creates all required indexes."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        # List all indexes
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in cursor.fetchall()}
        conn.close()
        
        expected_indexes = {
            "idx_findings_device",
            "idx_findings_expires",
            "idx_feedback_finding",
            "idx_feedback_device",
            "idx_feedback_timestamp",
        }
        
        assert expected_indexes.issubset(indexes)

    def test_init_db_is_idempotent(self, temp_db: Path) -> None:
        """Test that init_db is safe to call multiple times."""
        # Should not raise an error
        init_db(str(temp_db))
        init_db(str(temp_db))
        
        # Database should still be valid
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        count = cursor.fetchone()[0]
        conn.close()
        
        assert count >= 3


class TestFindingsTable:
    """Tests for findings table schema and constraints."""

    def test_insert_finding(self, temp_db: Path) -> None:
        """Test inserting a finding into the database."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc).isoformat()
        expires = datetime.now(timezone.utc).isoformat()
        
        cursor.execute(
            """
            INSERT INTO findings (id, device_id, finding_type, confidence, timestamp, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                "StepDuplication",
                0.95,
                now,
                expires,
            ),
        )
        conn.commit()
        
        # Verify insertion
        cursor.execute("SELECT * FROM findings WHERE id = ?", ("550e8400e29b41d4a716446655440000",))
        result = cursor.fetchone()
        conn.close()
        
        assert result is not None
        assert result[1] == "pixel_001"  # device_id
        assert result[2] == "StepDuplication"  # finding_type
        assert result[3] == 0.95  # confidence

    def test_confidence_check_constraint_too_high(self, temp_db: Path) -> None:
        """Test that confidence > 1.0 violates CHECK constraint."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc).isoformat()
        
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                """
                INSERT INTO findings (id, device_id, finding_type, confidence, timestamp, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "550e8400e29b41d4a716446655440000",
                    "pixel_001",
                    "StepDuplication",
                    1.5,  # Invalid: > 1.0
                    now,
                    now,
                ),
            )
        conn.close()

    def test_confidence_check_constraint_too_low(self, temp_db: Path) -> None:
        """Test that confidence < 0.0 violates CHECK constraint."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc).isoformat()
        
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                """
                INSERT INTO findings (id, device_id, finding_type, confidence, timestamp, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "550e8400e29b41d4a716446655440000",
                    "pixel_001",
                    "StepDuplication",
                    -0.1,  # Invalid: < 0.0
                    now,
                    now,
                ),
            )
        conn.close()

    def test_confidence_boundary_values(self, temp_db: Path) -> None:
        """Test that confidence = 0.0 and 1.0 are valid."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Insert with confidence = 0.0
        cursor.execute(
            """
            INSERT INTO findings (id, device_id, finding_type, confidence, timestamp, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                "StepDuplication",
                0.0,
                now,
                now,
            ),
        )
        
        # Insert with confidence = 1.0
        cursor.execute(
            """
            INSERT INTO findings (id, device_id, finding_type, confidence, timestamp, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "550e8400e29b41d4a716446655440001",
                "pixel_002",
                "ZeroErrorRates",
                1.0,
                now,
                now,
            ),
        )
        
        conn.commit()
        
        # Verify both were inserted
        cursor.execute("SELECT COUNT(*) FROM findings WHERE confidence IN (0.0, 1.0)")
        count = cursor.fetchone()[0]
        conn.close()
        
        assert count == 2


class TestFeedbackEventsTable:
    """Tests for feedback_events table schema and constraints."""

    def test_insert_feedback_event(self, temp_db: Path) -> None:
        """Test inserting a feedback event into the database."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc).isoformat()
        
        # First insert a finding
        cursor.execute(
            """
            INSERT INTO findings (id, device_id, finding_type, confidence, timestamp, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                "StepDuplication",
                0.95,
                now,
                now,
            ),
        )
        
        # Then insert a feedback event
        cursor.execute(
            """
            INSERT INTO feedback_events (id, finding_id, device_id, confirmed, evidence, confirmation_hash, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event_001",
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                1,
                "Device confirmed",
                "hash123",
                now,
            ),
        )
        conn.commit()
        
        # Verify insertion
        cursor.execute("SELECT * FROM feedback_events WHERE id = ?", ("event_001",))
        result = cursor.fetchone()
        conn.close()
        
        assert result is not None
        assert result[1] == "550e8400e29b41d4a716446655440000"  # finding_id
        assert result[2] == "pixel_001"  # device_id
        assert result[3] == 1  # confirmed

    def test_foreign_key_constraint_orphaned_confirmation(self, temp_db: Path) -> None:
        """Test that FOREIGN KEY constraint prevents orphaned feedback events."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        # Enable foreign key constraints
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Try to insert feedback event with non-existent finding_id
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                """
                INSERT INTO feedback_events (id, finding_id, device_id, confirmed, evidence, confirmation_hash, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "event_001",
                    "550e8400e29b41d4a716446655440000",  # Non-existent
                    "pixel_001",
                    1,
                    "Device confirmed",
                    "hash123",
                    now,
                ),
            )
        conn.close()

    def test_unique_constraint_finding_device_hash(self, temp_db: Path) -> None:
        """Test that UNIQUE constraint on (finding_id, device_id, confirmation_hash) works."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Insert a finding
        cursor.execute(
            """
            INSERT INTO findings (id, device_id, finding_type, confidence, timestamp, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                "StepDuplication",
                0.95,
                now,
                now,
            ),
        )
        
        # Insert first feedback event
        cursor.execute(
            """
            INSERT INTO feedback_events (id, finding_id, device_id, confirmed, evidence, confirmation_hash, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event_001",
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                1,
                "Confirmed",
                "hash123",
                now,
            ),
        )
        conn.commit()
        
        # Try to insert duplicate (same finding_id, device_id, confirmation_hash)
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                """
                INSERT INTO feedback_events (id, finding_id, device_id, confirmed, evidence, confirmation_hash, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "event_002",  # Different event ID
                    "550e8400e29b41d4a716446655440000",
                    "pixel_001",
                    0,
                    "Different evidence",
                    "hash123",  # Same hash = duplicate
                    now,
                ),
            )
        conn.close()

    def test_different_confirmation_hash_allowed(self, temp_db: Path) -> None:
        """Test that same finding+device with different hash is allowed."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Insert a finding
        cursor.execute(
            """
            INSERT INTO findings (id, device_id, finding_type, confidence, timestamp, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                "StepDuplication",
                0.95,
                now,
                now,
            ),
        )
        
        # Insert first feedback event
        cursor.execute(
            """
            INSERT INTO feedback_events (id, finding_id, device_id, confirmed, evidence, confirmation_hash, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event_001",
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                1,
                "Confirmed",
                "hash123",
                now,
            ),
        )
        
        # Insert second feedback event with different hash (should succeed)
        cursor.execute(
            """
            INSERT INTO feedback_events (id, finding_id, device_id, confirmed, evidence, confirmation_hash, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event_002",
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                0,
                "Different evidence",
                "hash456",  # Different hash
                now,
            ),
        )
        conn.commit()
        
        # Verify both were inserted
        cursor.execute(
            "SELECT COUNT(*) FROM feedback_events WHERE finding_id = ?",
            ("550e8400e29b41d4a716446655440000",),
        )
        count = cursor.fetchone()[0]
        conn.close()
        
        assert count == 2


class TestOrphanedConfirmationsTable:
    """Tests for orphaned_confirmations table."""

    def test_insert_orphaned_confirmation(self, temp_db: Path) -> None:
        """Test inserting an orphaned confirmation."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc).isoformat()
        
        cursor.execute(
            """
            INSERT INTO orphaned_confirmations (id, finding_id, device_id, payload, error_message, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "orphan_001",
                "550e8400e29b41d4a716446655440000",
                "pixel_001",
                '{"confirmed": true}',
                "Finding not found",
                now,
            ),
        )
        conn.commit()
        
        # Verify insertion
        cursor.execute("SELECT * FROM orphaned_confirmations WHERE id = ?", ("orphan_001",))
        result = cursor.fetchone()
        conn.close()
        
        assert result is not None
        assert result[1] == "550e8400e29b41d4a716446655440000"  # finding_id


class TestDatabaseIndexes:
    """Tests for database indexes."""

    def test_indexes_exist(self, temp_db: Path) -> None:
        """Test that all expected indexes exist."""
        init_db(str(temp_db))
        
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        
        expected_indexes = [
            "idx_findings_device",
            "idx_findings_expires",
            "idx_feedback_finding",
            "idx_feedback_device",
            "idx_feedback_timestamp",
        ]
        
        for index_name in expected_indexes:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            )
            result = cursor.fetchone()
            assert result is not None, f"Index {index_name} not found"
        
        conn.close()
