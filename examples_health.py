#!/usr/bin/env python3
"""Example: Using the deep_think_mcp health endpoint.

This example shows how to:
1. Query the health endpoint
2. Parse and interpret the response
3. Use health metrics for monitoring and alerting
"""

import asyncio
import json
from datetime import datetime

# Example health endpoint response
HEALTH_RESPONSE_EXAMPLE = {
    "status": "healthy",
    "http_status": 200,
    "timestamp": "2024-01-01T12:00:00.000000+00:00",
    "pending_count": 5,
    "avg_latency": 10.5,
    "last_success_timestamp": "2024-01-01T11:59:30.000000+00:00",
    "worker_count": 1,
    "db_status": "healthy",
    "completed_count": 100
}

HEALTH_RESPONSE_DEGRADED = {
    "status": "degraded",
    "http_status": 503,
    "timestamp": "2024-01-01T12:00:00.000000+00:00",
    "pending_count": 150,
    "avg_latency": 10.5,
    "last_success_timestamp": "2024-01-01T11:59:30.000000+00:00",
    "worker_count": 1,
    "db_status": "healthy",
    "completed_count": 100,
    "reason": "Too many pending jobs (150 >= 100)"
}


def example_basic_check():
    """Example 1: Basic health check."""
    print("=" * 60)
    print("EXAMPLE 1: Basic Health Check")
    print("=" * 60)
    
    # In a real application, you would use:
    # response = requests.get("http://localhost:8080/health")
    # health = response.json()
    
    health = HEALTH_RESPONSE_EXAMPLE
    
    print(f"\nStatus: {health['status'].upper()}")
    print(f"HTTP Status: {health['http_status']}")
    print(f"Pending Jobs: {health['pending_count']}")
    print(f"Average Latency: {health['avg_latency']:.1f}s")
    print(f"Total Completed: {health['completed_count']}")
    print(f"Database: {health['db_status']}")


def example_monitoring():
    """Example 2: Health monitoring and alerting."""
    print("\n" + "=" * 60)
    print("EXAMPLE 2: Monitoring and Alerting")
    print("=" * 60)
    
    health = HEALTH_RESPONSE_EXAMPLE
    
    # Define thresholds
    MAX_PENDING = 100
    MAX_AVG_LATENCY = 30.0  # seconds
    
    alerts = []
    
    # Check pending jobs
    if health["pending_count"] >= MAX_PENDING:
        alerts.append(f"⚠️  High queue depth: {health['pending_count']} jobs pending")
    
    # Check average latency
    if health["avg_latency"] > MAX_AVG_LATENCY:
        alerts.append(f"⚠️  High latency: {health['avg_latency']:.1f}s average")
    
    # Check database
    if health["db_status"] != "healthy":
        alerts.append(f"🔴 Database issue: {health['db_status']}")
    
    # Check if service is responsive
    if health["status"] != "healthy":
        alerts.append(f"🔴 Service degraded: {health.get('reason', 'unknown')}")
    
    if alerts:
        print("\nAlerts:")
        for alert in alerts:
            print(f"  {alert}")
    else:
        print("\n✅ No alerts - system is operating normally")


def example_degraded_status():
    """Example 3: Handling degraded status."""
    print("\n" + "=" * 60)
    print("EXAMPLE 3: Handling Degraded Status")
    print("=" * 60)
    
    health = HEALTH_RESPONSE_DEGRADED
    
    print(f"\nStatus: {health['status'].upper()}")
    print(f"HTTP Status: {health['http_status']}")
    
    if health["status"] != "healthy":
        print(f"\nReason: {health.get('reason', 'Unknown')}")
        
        # Take action based on reason
        if "pending" in health.get("reason", "").lower():
            print("\nRecommendation: Scale up workers or check for job failures")
        elif "database" in health.get("reason", "").lower():
            print("\nRecommendation: Check database connectivity and disk space")
        else:
            print("\nRecommendation: Check server logs for more details")


def example_load_balancer_integration():
    """Example 4: Load balancer integration."""
    print("\n" + "=" * 60)
    print("EXAMPLE 4: Load Balancer Integration")
    print("=" * 60)
    
    print("""
In Kubernetes, use the health endpoint for:

1. Liveness Probe (pod restart):
   livenessProbe:
     httpGet:
       path: /health
       port: 8080
     initialDelaySeconds: 10
     periodSeconds: 30
     failureThreshold: 3

2. Readiness Probe (traffic routing):
   readinessProbe:
     httpGet:
       path: /health
       port: 8080
     initialDelaySeconds: 5
     periodSeconds: 10
     failureThreshold: 2

The probe considers the HTTP status code:
- 200 OK (healthy):      Pod is ready for traffic
- 503 Service Unavailable (degraded): Remove from load balancer
""")


def example_metrics_tracking():
    """Example 5: Track metrics over time."""
    print("\n" + "=" * 60)
    print("EXAMPLE 5: Metrics Tracking")
    print("=" * 60)
    
    # Simulate collecting metrics over time
    health_samples = [
        {
            "timestamp": "2024-01-01T12:00:00Z",
            "pending_count": 5,
            "avg_latency": 10.5,
        },
        {
            "timestamp": "2024-01-01T12:01:00Z",
            "pending_count": 15,
            "avg_latency": 11.2,
        },
        {
            "timestamp": "2024-01-01T12:02:00Z",
            "pending_count": 25,
            "avg_latency": 12.1,
        },
    ]
    
    print("\nMetrics over time:")
    print(f"{'Time':<25} {'Pending':<12} {'Avg Latency':<12}")
    print("-" * 50)
    
    for sample in health_samples:
        print(f"{sample['timestamp']:<25} {sample['pending_count']:<12} {sample['avg_latency']:<12.1f}s")
    
    # Calculate trends
    avg_pending = sum(s["pending_count"] for s in health_samples) / len(health_samples)
    print(f"\nAverage pending jobs: {avg_pending:.0f}")


def example_curl_commands():
    """Example 6: Using curl to query the health endpoint."""
    print("\n" + "=" * 60)
    print("EXAMPLE 6: curl Commands")
    print("=" * 60)
    
    print("""
# Basic health check
curl http://localhost:8080/health

# Pretty-print JSON response
curl -s http://localhost:8080/health | jq .

# Extract specific field (pending jobs)
curl -s http://localhost:8080/health | jq '.pending_count'

# Check HTTP status code
curl -s -o /dev/null -w "%{http_code}\\n" http://localhost:8080/health

# Monitor health every second
watch -n 1 'curl -s http://localhost:8080/health | jq .'

# Export to Prometheus format
curl -s http://localhost:8080/health | jq -r '.pending_count as $p | 
  "deep_think_pending_jobs " + ($p | tostring)'
""")


if __name__ == "__main__":
    example_basic_check()
    example_monitoring()
    example_degraded_status()
    example_load_balancer_integration()
    example_metrics_tracking()
    example_curl_commands()
    
    print("\n" + "=" * 60)
    print("✅ Examples complete!")
    print("=" * 60)
