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

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_findings_device ON findings(device_id);
CREATE INDEX IF NOT EXISTS idx_findings_expires ON findings(expires_at);
CREATE INDEX IF NOT EXISTS idx_feedback_finding ON feedback_events(finding_id);
CREATE INDEX IF NOT EXISTS idx_feedback_device ON feedback_events(device_id);
CREATE INDEX IF NOT EXISTS idx_feedback_timestamp ON feedback_events(timestamp);
