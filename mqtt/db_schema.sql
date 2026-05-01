-- MQTT Feedback Loop Database Schema
-- Handles findings from device telemetry and confirmations from devices

-- Findings table: stores anomalies detected in device telemetry
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    deleted_at DATETIME NULL,
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

-- Feedback events table: stores device confirmations of findings
CREATE TABLE IF NOT EXISTS feedback_events (
    id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    confirmed INTEGER NOT NULL,
    evidence TEXT,
    confirmation_hash TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (finding_id) REFERENCES findings(id) ON DELETE CASCADE,
    UNIQUE (finding_id, device_id, confirmation_hash)
);

-- Orphaned confirmations table: stores confirmations for findings that don't exist
CREATE TABLE IF NOT EXISTS orphaned_confirmations (
    id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    error_message TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Correlations table: stores multi-sensor environment fingerprints
CREATE TABLE IF NOT EXISTS correlations (
    id TEXT PRIMARY KEY,
    timestamp DATETIME NOT NULL,
    location_hash TEXT NOT NULL,
    observing_devices TEXT NOT NULL,  -- JSON array of device IDs
    sensor_snapshot TEXT NOT NULL,    -- JSON object of aggregated sensor values
    novelty_score REAL NOT NULL,
    fleet_prevalence REAL NOT NULL,
    entropy_breakdown TEXT NOT NULL,  -- JSON object of per-sensor entropy
    is_anomalous_cluster INTEGER NOT NULL,
    anomaly_details TEXT,             -- JSON object of anomaly details
    confidence REAL DEFAULT 0.8,
    expires_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CHECK (novelty_score >= 0.0 AND novelty_score <= 1.0),
    CHECK (fleet_prevalence >= 0.0 AND fleet_prevalence <= 1.0),
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

-- Correlation annotations table: post-event human annotations on correlations
CREATE TABLE IF NOT EXISTS correlation_annotations (
    id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    annotator_id TEXT NOT NULL,
    label TEXT NOT NULL,              -- e.g., "RF_hacking_village", "elevator", "convention_floor_2"
    evidence TEXT,
    confidence REAL DEFAULT 0.8,
    annotated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (correlation_id) REFERENCES correlations(id) ON DELETE CASCADE
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_findings_device ON findings(device_id);
CREATE INDEX IF NOT EXISTS idx_findings_expires ON findings(expires_at);
CREATE INDEX IF NOT EXISTS idx_feedback_finding ON feedback_events(finding_id);
CREATE INDEX IF NOT EXISTS idx_feedback_device ON feedback_events(device_id);
CREATE INDEX IF NOT EXISTS idx_feedback_timestamp ON feedback_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_correlations_location ON correlations(location_hash);
CREATE INDEX IF NOT EXISTS idx_correlations_timestamp ON correlations(timestamp);
CREATE INDEX IF NOT EXISTS idx_correlations_novelty ON correlations(novelty_score);
CREATE INDEX IF NOT EXISTS idx_correlations_expires ON correlations(expires_at);
CREATE INDEX IF NOT EXISTS idx_annotations_correlation ON correlation_annotations(correlation_id);
