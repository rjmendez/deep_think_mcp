"""Real-time multi-sensor correlation detection and novelty scoring.

This module provides CorrelationEngine, which:
  1. Subscribes to MQTT findings stream (dama/+/findings)
  2. Windows findings by time (5-10 sec) and location (~10m radius)
  3. Calculates multi-sensor entropy for novelty scoring
  4. Detects co-location anomalies (devices diverging)
  5. Publishes CorrelationFindings to MQTT + database
  6. Tracks fleet history for prevalence calculation

The correlation engine is the bridge between individual ant sensor findings
and multi-sensor environment fingerprints that reveal unknown patterns.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any, Set
from collections import defaultdict
import math
import uuid

from .models import Finding, CorrelationFinding, ValidationError


logger = logging.getLogger(__name__)


class LocationBucket:
    """Represents a spatial-temporal bucket for findings aggregation."""
    
    def __init__(self, location_hash: str, window_start: datetime, 
                 window_duration_sec: int = 10):
        self.location_hash = location_hash
        self.window_start = window_start
        self.window_duration_sec = window_duration_sec
        self.findings: List[Finding] = []
        self.last_update = datetime.now(timezone.utc)
    
    def is_expired(self, now: datetime) -> bool:
        """Check if window has expired (no updates for duration)."""
        return (now - self.last_update).total_seconds() > self.window_duration_sec
    
    def add_finding(self, finding: Finding) -> None:
        """Add a finding to this bucket."""
        self.findings.append(finding)
        self.last_update = datetime.now(timezone.utc)
    
    def get_device_ids(self) -> Set[str]:
        """Get unique device IDs observing this location."""
        return {f.device_id for f in self.findings}
    
    def is_ready(self, min_devices: int = 2) -> Tuple[bool, str]:
        """Check if window is ready to correlate.
        
        Returns:
            (is_ready, reason) tuple
        """
        devices = self.get_device_ids()
        now = datetime.now(timezone.utc)
        elapsed = (now - self.window_start).total_seconds()
        
        # Ready if: enough devices + minimum time elapsed OR window duration exceeded
        if len(devices) >= min_devices and elapsed >= 2.0:
            return True, f"{len(devices)} devices after {elapsed:.1f}s"
        if elapsed >= self.window_duration_sec:
            return True, f"window timeout after {elapsed:.1f}s"
        
        return False, f"{len(devices)} devices, {elapsed:.1f}s elapsed"


class CorrelationEngine:
    """Real-time multi-sensor correlation detector and novelty scorer.
    
    Maintains sliding windows of findings aggregated by location + time,
    calculates entropy-based novelty scores, and tracks fleet history.
    """
    
    def __init__(self, 
                 time_window_sec: int = 10,
                 location_radius_m: int = 10,
                 min_devices_for_correlation: int = 2):
        """Initialize the correlation engine.
        
        Args:
            time_window_sec: Aggregation window duration (default 10 sec)
            location_radius_m: Spatial clustering radius (default 10m)
            min_devices_for_correlation: Minimum devices to trigger correlation
        """
        self.time_window_sec = time_window_sec
        self.location_radius_m = location_radius_m
        self.min_devices_for_correlation = min_devices_for_correlation
        
        # Sliding window: location_hash -> LocationBucket
        self.location_buckets: Dict[str, LocationBucket] = {}
        
        # Fleet history: sensor_fingerprint_hash -> (count, first_seen, last_seen)
        self.fleet_history: Dict[str, Tuple[int, datetime, datetime]] = {}
        
        # Sensor type weights for entropy calculation (0-1, higher = more important)
        self.sensor_weights = {
            "wifi_ssids": 0.95,           # WiFi networks are very distinctive
            "temperature_bin": 0.60,       # Temperature varies by location
            "humidity_bin": 0.50,          # Humidity correlates with environment
            "audio_bins": 0.75,            # Audio fingerprint is environment-specific
            "light_level": 0.40,           # Light varies indoors/outdoors
            "imu_vibrations": 0.55,        # Vibration patterns distinctive
            "air_pressure": 0.65,          # Pressure changes with elevation/indoors
            "bluetooth_count": 0.50,       # Bluetooth device count varies
            "cellular_quality": 0.70,      # Cell signal varies with location
            "packet_types": 0.80,          # Network types observed
        }
    
    async def on_finding(self, finding: Finding) -> Optional[CorrelationFinding]:
        """Process a new finding, return correlation if window is ready.
        
        Args:
            finding: The finding to process
            
        Returns:
            CorrelationFinding if a window was completed, else None
        """
        try:
            location_hash = self._extract_location_hash(finding)
            
            # Initialize or get bucket for this location
            if location_hash not in self.location_buckets:
                self.location_buckets[location_hash] = LocationBucket(
                    location_hash,
                    datetime.now(timezone.utc),
                    self.time_window_sec
                )
            
            bucket = self.location_buckets[location_hash]
            bucket.add_finding(finding)
            
            # Check if any buckets are ready to correlate
            correlation = None
            if bucket.is_ready(self.min_devices_for_correlation)[0]:
                correlation = await self._correlate_window(location_hash, bucket)
                # Remove processed bucket
                del self.location_buckets[location_hash]
            
            # Clean up expired buckets
            self._cleanup_expired_buckets()
            
            return correlation
        
        except Exception as e:
            logger.error(f"Error processing finding: {e}", exc_info=True)
            return None
    
    async def _correlate_window(self, location_hash: str, 
                                 bucket: LocationBucket) -> CorrelationFinding:
        """Aggregate findings in a window and create correlation.
        
        Args:
            location_hash: The location being correlated
            bucket: The bucket containing findings
            
        Returns:
            CorrelationFinding with novelty scores
        """
        devices = bucket.get_device_ids()
        
        # Extract sensor snapshot: merge and bin sensor readings
        sensor_snapshot = self._aggregate_sensor_snapshot(bucket.findings)
        
        # Calculate entropy and novelty score
        novelty_score, entropy_breakdown = self._calculate_entropy(sensor_snapshot)
        
        # Check fleet history for prevalence
        fleet_prevalence = self._calculate_fleet_prevalence(sensor_snapshot)
        
        # Detect co-location anomalies (devices diverging)
        is_anomalous, anomaly_details = self._detect_anomalies(bucket.findings)
        
        # Update fleet history
        self._update_fleet_history(sensor_snapshot)
        
        # Create correlation
        correlation = CorrelationFinding(
            id=str(uuid.uuid4()),
            timestamp=bucket.window_start.isoformat() + "Z",
            location_hash=location_hash,
            observing_devices=list(devices),
            sensor_snapshot=sensor_snapshot,
            novelty_score=novelty_score,
            fleet_prevalence=fleet_prevalence,
            entropy_breakdown=entropy_breakdown,
            is_anomalous_cluster=is_anomalous,
            anomaly_details=anomaly_details,
            confidence=0.8 if len(devices) >= 2 else 0.6,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat() + "Z"
        )
        
        logger.info(
            f"Correlated {len(bucket.findings)} findings from {len(devices)} devices "
            f"at {location_hash}: novelty={correlation.novelty_score:.3f}, "
            f"prevalence={fleet_prevalence:.1%}"
        )
        
        return correlation
    
    def _extract_location_hash(self, finding: Finding) -> str:
        """Extract and hash location from finding.
        
        Uses GPS if available, falls back to WiFi location or device ID.
        
        Args:
            finding: The finding with potential GPS/location metadata
            
        Returns:
            Location hash for spatial bucketing
        """
        metadata = finding.metadata or {}
        
        # Try GPS first (most precise)
        gps = metadata.get("gps")
        if gps and isinstance(gps, dict):
            lat = gps.get("latitude")
            lon = gps.get("longitude")
            if lat is not None and lon is not None:
                # Round to ~10m grid (0.0001 degrees ≈ 10m at equator)
                lat_bucket = round(lat, 4)
                lon_bucket = round(lon, 4)
                return f"gps_{lat_bucket}_{lon_bucket}"
        
        # Try WiFi AP location (if known)
        wifi_aps = metadata.get("wifi_networks", [])
        if wifi_aps:
            # Use strongest AP's SSID as location hint
            ap_hash = hashlib.md5(str(wifi_aps[0]).encode()).hexdigest()[:8]
            return f"wifi_{ap_hash}"
        
        # Fallback: device location (less precise but works)
        return f"device_{finding.device_id}"
    
    def _aggregate_sensor_snapshot(self, findings: List[Finding]) -> Dict[str, Any]:
        """Aggregate sensor readings from multiple findings into snapshot.
        
        Merges WiFi SSIDs, takes median/mode of numeric values.
        
        Args:
            findings: List of findings to aggregate
            
        Returns:
            Dict with merged sensor values
        """
        snapshot = {
            "wifi_ssids": [],
            "temperature_bin": None,
            "humidity_bin": None,
            "audio_bins": [],
            "light_level": None,
            "imu_vibrations": {},
            "air_pressure": None,
            "bluetooth_count": 0,
            "cellular_quality": None,
            "packet_types": [],
            "device_count": len({f.device_id for f in findings}),
            "finding_count": len(findings),
        }
        
        temps = []
        humidities = []
        light_levels = []
        pressure_values = []
        
        # Aggregate sensor values across findings
        for finding in findings:
            meta = finding.metadata or {}
            
            # WiFi: collect and deduplicate SSIDs
            if "wifi_networks" in meta:
                for ssid in meta["wifi_networks"]:
                    if ssid not in snapshot["wifi_ssids"]:
                        snapshot["wifi_ssids"].append(ssid)
            
            # Temperature: collect for median later
            if "temperature" in meta:
                temps.append(meta["temperature"])
            
            # Humidity: collect for median
            if "humidity" in meta:
                humidities.append(meta["humidity"])
            
            # Audio: aggregate bins
            if "audio_bins" in meta:
                for bin_val in meta["audio_bins"]:
                    if bin_val not in snapshot["audio_bins"]:
                        snapshot["audio_bins"].append(bin_val)
            
            # Light: collect for median
            if "light_level" in meta:
                light_levels.append(meta["light_level"])
            
            # IMU: aggregate frequency bands
            if "imu_vibrations" in meta:
                for freq, val in meta["imu_vibrations"].items():
                    if freq not in snapshot["imu_vibrations"]:
                        snapshot["imu_vibrations"][freq] = []
                    snapshot["imu_vibrations"][freq].append(val)
            
            # Pressure: collect for median
            if "air_pressure" in meta:
                pressure_values.append(meta["air_pressure"])
            
            # Bluetooth: sum device counts
            if "bluetooth_count" in meta:
                snapshot["bluetooth_count"] += meta["bluetooth_count"]
            
            # Cellular: use best quality observed
            if "cellular_quality" in meta:
                snapshot["cellular_quality"] = meta["cellular_quality"]
            
            # Packet types: collect and deduplicate
            if "packet_types" in meta:
                for ptype in meta["packet_types"]:
                    if ptype not in snapshot["packet_types"]:
                        snapshot["packet_types"].append(ptype)
        
        # Calculate binned values from aggregated data
        snapshot["temperature_bin"] = self._bin_numeric("temperature", temps)
        snapshot["humidity_bin"] = self._bin_numeric("humidity", humidities)
        snapshot["light_level"] = self._bin_numeric("light", light_levels)
        snapshot["air_pressure"] = self._bin_numeric("pressure", pressure_values)
        
        # Average IMU vibration values per frequency band
        for freq, vals in snapshot["imu_vibrations"].items():
            if vals:
                snapshot["imu_vibrations"][freq] = sum(vals) / len(vals)
        
        return snapshot
    
    def _bin_numeric(self, sensor_type: str, values: List[float]) -> Optional[str]:
        """Bin numeric sensor values into categories.
        
        Args:
            sensor_type: Type of sensor (temperature, humidity, light, pressure)
            values: List of numeric readings
            
        Returns:
            String bin identifier, or None if no values
        """
        if not values:
            return None
        
        median_val = sorted(values)[len(values) // 2]
        
        if sensor_type == "temperature":
            # Bins: <16, 16-20, 20-24, 24-28, 28-32, >32°C
            if median_val < 16:
                return "temp_cold"
            elif median_val < 20:
                return "temp_cool"
            elif median_val < 24:
                return "temp_neutral"
            elif median_val < 28:
                return "temp_warm"
            elif median_val < 32:
                return "temp_hot"
            else:
                return "temp_extreme"
        
        elif sensor_type == "humidity":
            # Bins: <20%, 20-40%, 40-60%, 60-80%, >80%
            if median_val < 20:
                return "humidity_dry"
            elif median_val < 40:
                return "humidity_low"
            elif median_val < 60:
                return "humidity_moderate"
            elif median_val < 80:
                return "humidity_high"
            else:
                return "humidity_very_high"
        
        elif sensor_type == "light":
            # Bins: dark, dim, normal, bright
            if median_val < 100:
                return "light_dark"
            elif median_val < 500:
                return "light_dim"
            elif median_val < 2000:
                return "light_normal"
            else:
                return "light_bright"
        
        elif sensor_type == "pressure":
            # Bins: low, normal, high (sea level ~1013 hPa)
            if median_val < 950:
                return "pressure_low"
            elif median_val < 1013:
                return "pressure_below_sea_level"
            elif median_val < 1100:
                return "pressure_normal"
            else:
                return "pressure_high"
        
        return None
    
    def _calculate_entropy(self, sensor_snapshot: Dict[str, Any]
                          ) -> Tuple[float, Dict[str, float]]:
        """Calculate multi-sensor entropy for novelty scoring.
        
        Each sensor type contributes independently via Shannon entropy.
        Weighted average across sensors determines novelty_score.
        
        Args:
            sensor_snapshot: Aggregated sensor values
            
        Returns:
            (novelty_score, entropy_breakdown) tuple
        """
        entropy_scores = {}
        total_weight = 0.0
        weighted_entropy = 0.0
        
        for sensor_type, weight in self.sensor_weights.items():
            if sensor_type not in sensor_snapshot:
                continue
            
            value = sensor_snapshot[sensor_type]
            if value is None:
                continue
            
            # Calculate entropy for this sensor
            entropy = self._calculate_sensor_entropy(sensor_type, value)
            entropy_scores[sensor_type] = entropy
            
            weighted_entropy += entropy * weight
            total_weight += weight
        
        # Normalize by total weight
        if total_weight > 0:
            novelty_score = weighted_entropy / total_weight
        else:
            novelty_score = 0.0
        
        return novelty_score, entropy_scores
    
    def _calculate_sensor_entropy(self, sensor_type: str, value: Any) -> float:
        """Calculate Shannon entropy for a single sensor reading.
        
        Uses fleet history to determine how common this value is.
        
        Args:
            sensor_type: Type of sensor
            value: Current sensor value/bin
            
        Returns:
            Entropy score (0-1, higher = more novel)
        """
        # For simplicity, use occurrence frequency in fleet history
        # More sophisticated: use probability distribution from history
        
        sensor_key = f"{sensor_type}_{value}"
        
        # Count occurrences in fleet history
        occurrences = sum(1 for k, v in self.fleet_history.items() 
                         if sensor_type in k and value in str(v))
        
        total_observations = len(self.fleet_history)
        
        if total_observations == 0:
            # No history yet: maximum novelty
            return 1.0
        
        # Probability of this sensor value in fleet history
        p = max(0.001, occurrences / total_observations)  # Min 0.1% to avoid log(0)
        
        # Shannon entropy: -p * log2(p)
        # High p (common) = low entropy
        # Low p (rare) = high entropy
        entropy = -p * math.log2(p)
        
        # Normalize to 0-1 range (max entropy for uniform dist is 1.0)
        return min(1.0, entropy)
    
    def _calculate_fleet_prevalence(self, sensor_snapshot: Dict[str, Any]) -> float:
        """Calculate what % of fleet has seen this fingerprint before.
        
        Args:
            sensor_snapshot: Aggregated sensor values
            
        Returns:
            Prevalence score (0.0 = never seen, 1.0 = everyone has seen)
        """
        fingerprint_hash = self._fingerprint_hash(sensor_snapshot)
        
        if fingerprint_hash not in self.fleet_history:
            return 0.0  # Never seen before
        
        count, _, _ = self.fleet_history[fingerprint_hash]
        
        # Estimate: if seen N times by different devices, prevalence = count/expected_max
        # Expected max: 6 phones * ~1000 observations per DefCon ≈ 6000 total
        max_expected = 6000
        
        prevalence = min(1.0, count / max_expected)
        return prevalence
    
    def _detect_anomalies(self, findings: List[Finding]
                         ) -> Tuple[bool, Dict[str, Any]]:
        """Detect co-location anomalies (devices diverging significantly).
        
        Flags when two phones at same location report vastly different readings.
        
        Args:
            findings: List of findings at same location
            
        Returns:
            (is_anomalous, details) tuple
        """
        if len(findings) < 2:
            return False, {}
        
        anomalies = {}
        
        # Compare temperatures
        temps = [f.metadata.get("temperature") for f in findings 
                if f.metadata and "temperature" in f.metadata]
        if len(temps) >= 2 and None not in temps:
            max_temp = max(temps)
            min_temp = min(temps)
            if max_temp - min_temp > 5.0:  # >5°C difference = anomalous
                anomalies["temperature"] = {
                    "max": max_temp, "min": min_temp, "diff": max_temp - min_temp
                }
        
        # Compare WiFi networks seen
        wifi_sets = []
        for f in findings:
            if f.metadata and "wifi_networks" in f.metadata:
                wifi_sets.append(set(f.metadata["wifi_networks"]))
        
        if len(wifi_sets) >= 2:
            # Check set overlap
            union = set()
            for ws in wifi_sets:
                union.update(ws)
            
            intersection = wifi_sets[0]
            for ws in wifi_sets[1:]:
                intersection = intersection.intersection(ws)
            
            overlap_pct = len(intersection) / len(union) if union else 0
            if overlap_pct < 0.5:  # <50% overlap = anomalous
                anomalies["wifi_networks"] = {
                    "overlap_pct": overlap_pct,
                    "devices_count": len(wifi_sets),
                }
        
        # Compare cellular quality
        cell_qualities = [f.metadata.get("cellular_quality") for f in findings 
                         if f.metadata and "cellular_quality" in f.metadata]
        if len(cell_qualities) >= 2 and None not in cell_qualities:
            # Convert to numeric (e.g., "excellent"=4, "poor"=0)
            quality_map = {"poor": 0, "fair": 1, "good": 2, "excellent": 3}
            numeric_qualities = [quality_map.get(q, 0) for q in cell_qualities]
            if max(numeric_qualities) - min(numeric_qualities) >= 2:  # 2+ bars diff
                anomalies["cellular_quality"] = {
                    "qualities": cell_qualities,
                    "max_diff": max(numeric_qualities) - min(numeric_qualities)
                }
        
        is_anomalous = len(anomalies) > 0
        return is_anomalous, anomalies
    
    def _update_fleet_history(self, sensor_snapshot: Dict[str, Any]) -> None:
        """Update fleet history with new sensor reading.
        
        Args:
            sensor_snapshot: Aggregated sensor values to record
        """
        fingerprint_hash = self._fingerprint_hash(sensor_snapshot)
        now = datetime.now(timezone.utc)
        
        if fingerprint_hash in self.fleet_history:
            count, first_seen, _ = self.fleet_history[fingerprint_hash]
            self.fleet_history[fingerprint_hash] = (count + 1, first_seen, now)
        else:
            self.fleet_history[fingerprint_hash] = (1, now, now)
    
    def _fingerprint_hash(self, sensor_snapshot: Dict[str, Any]) -> str:
        """Create deterministic hash of sensor snapshot for fleet history.
        
        Args:
            sensor_snapshot: Aggregated sensor values
            
        Returns:
            Hex hash (MD5) of fingerprint
        """
        # Build fingerprint from key sensor values
        parts = []
        
        for key in sorted(self.sensor_weights.keys()):
            if key in sensor_snapshot:
                value = sensor_snapshot[key]
                if value is not None:
                    # Convert to string for hashing
                    if isinstance(value, (list, dict)):
                        value_str = str(sorted(str(v) for v in value))
                    else:
                        value_str = str(value)
                    parts.append(f"{key}={value_str}")
        
        fingerprint = "|".join(parts)
        return hashlib.md5(fingerprint.encode()).hexdigest()
    
    def _cleanup_expired_buckets(self) -> None:
        """Remove expired buckets (no updates for window duration)."""
        now = datetime.now(timezone.utc)
        expired = [loc for loc, bucket in self.location_buckets.items()
                  if bucket.is_expired(now)]
        
        for loc in expired:
            del self.location_buckets[loc]
            if expired:
                logger.debug(f"Cleaned up {len(expired)} expired location buckets")
