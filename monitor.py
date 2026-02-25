#!/usr/bin/env python3
"""
KukuiBot Performance Monitor & Benchmarking Tool

Continuously monitors KukuiBot at https://kukuibot.example.com:7000
Tracks:
- Response times (latency percentiles)
- Error rates and types
- Throughput (requests/sec)
- Resource usage (CPU, memory)
- Model availability and performance
- Endpoint-specific metrics

Results logged to ~/.kukuibot/logs/monitor.log and daily reports in ~/.kukuibot/monitor/
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import ssl
import statistics
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# Configuration
KUKUIBOT_URL = "https://kukuibot.example.com:7000"
MONITOR_HOME = Path(os.path.expanduser("~/.kukuibot/monitor"))
LOGS_DIR = Path(os.path.expanduser("~/.kukuibot/logs"))
MONITOR_LOG = LOGS_DIR / "monitor.log"

# Monitoring intervals
CHECK_INTERVAL = 30  # seconds between checks
REPORT_INTERVAL = 300  # seconds between detailed reports (5 min)
DAILY_REPORT_INTERVAL = 86400  # seconds for daily summary

# SSL context (skip verification for self-signed certs)
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

# Endpoints to monitor
# Note: Some endpoints require auth; we measure latency regardless of status
ENDPOINTS = [
    ("GET", "/", "home", 1.0),  # weight for importance
    ("GET", "/settings", "settings", 0.8),
    ("GET", "/health", "health", 1.0),  # public health check
    ("GET", "/api/gmail/status", "gmail_status", 1.0),
    ("GET", "/api/reports/config", "reports_config", 0.9),
    ("POST", "/api/reports/list", "reports_list", 0.8),
    ("POST", "/api/chat", "chat", 2.0),  # highest weight - critical path
]

# Setup logging
MONITOR_HOME.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(MONITOR_LOG),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


@dataclass
class Metric:
    """Single metric measurement"""
    timestamp: str
    endpoint: str
    method: str
    status_code: int | None
    response_time_ms: float
    error: str | None = None
    payload_size_bytes: int | None = None
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AggregateMetrics:
    """Aggregated metrics over a time window"""
    window_start: str
    window_end: str
    endpoint: str
    method: str
    count: int
    avg_response_time_ms: float
    p50_response_time_ms: float
    p95_response_time_ms: float
    p99_response_time_ms: float
    error_count: int
    error_rate: float
    success_rate: float
    
    def to_dict(self) -> dict:
        return asdict(self)


class MetricBuffer:
    """Circular buffer for recent metrics"""
    def __init__(self, max_size: int = 10000):
        self.buffer = deque(maxlen=max_size)
        self.lock = asyncio.Lock()
    
    async def add(self, metric: Metric) -> None:
        async with self.lock:
            self.buffer.append(metric)
    
    async def get_all(self) -> list[Metric]:
        async with self.lock:
            return list(self.buffer)
    
    async def get_recent(self, seconds: int) -> list[Metric]:
        """Get metrics from last N seconds"""
        async with self.lock:
            cutoff = dt.datetime.now() - dt.timedelta(seconds=seconds)
            return [
                m for m in self.buffer
                if dt.datetime.fromisoformat(m.timestamp) > cutoff
            ]


class Monitor:
    """KukuiBot performance monitor"""
    
    def __init__(self, url: str = KUKUIBOT_URL):
        self.url = url
        self.buffer = MetricBuffer()
        self.last_report_time = time.time()
        self.last_daily_report_time = time.time()
        self.session_start = dt.datetime.now()
        
    async def check_endpoint(
        self,
        method: str,
        path: str,
        endpoint_name: str,
    ) -> Metric:
        """Check a single endpoint and record metric"""
        full_url = f"{self.url}{path}"
        start_time = time.time()
        status_code = None
        error = None
        payload_size = None
        
        try:
            req = urllib.request.Request(
                full_url,
                method=method,
                headers={"User-Agent": "KukuiBot-Monitor/1.0"},
            )
            
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=10) as response:
                status_code = response.status
                payload = response.read()
                payload_size = len(payload)
                
        except urllib.error.HTTPError as e:
            status_code = e.code
            error = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            error = f"Connection error: {e.reason}"
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)}"
        
        response_time_ms = (time.time() - start_time) * 1000
        
        metric = Metric(
            timestamp=dt.datetime.now().isoformat(),
            endpoint=endpoint_name,
            method=method,
            status_code=status_code,
            response_time_ms=response_time_ms,
            error=error,
            payload_size_bytes=payload_size,
        )
        
        await self.buffer.add(metric)
        return metric
    
    async def run_check_cycle(self) -> None:
        """Run one cycle of endpoint checks"""
        tasks = [
            self.check_endpoint(method, path, name)
            for method, path, name, _ in ENDPOINTS
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Log individual results
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Check failed: {result}")
            elif result.error:
                logger.warning(
                    f"{result.endpoint} ({result.method}): {result.error} "
                    f"({result.response_time_ms:.1f}ms)"
                )
            else:
                logger.info(
                    f"{result.endpoint} ({result.method}): "
                    f"{result.status_code} ({result.response_time_ms:.1f}ms)"
                )
    
    async def generate_report(self, window_seconds: int = 300) -> dict:
        """Generate aggregated metrics report"""
        metrics = await self.buffer.get_recent(window_seconds)
        
        if not metrics:
            return {"error": "No metrics available"}
        
        # Group by endpoint
        by_endpoint = defaultdict(list)
        for m in metrics:
            by_endpoint[m.endpoint].append(m)
        
        aggregates = []
        for endpoint, endpoint_metrics in by_endpoint.items():
            response_times = [m.response_time_ms for m in endpoint_metrics]
            errors = [m for m in endpoint_metrics if m.error]
            
            # Calculate percentiles
            response_times_sorted = sorted(response_times)
            count = len(response_times_sorted)
            
            agg = AggregateMetrics(
                window_start=metrics[0].timestamp,
                window_end=metrics[-1].timestamp,
                endpoint=endpoint,
                method=endpoint_metrics[0].method,
                count=len(endpoint_metrics),
                avg_response_time_ms=statistics.mean(response_times),
                p50_response_time_ms=response_times_sorted[int(count * 0.50)],
                p95_response_time_ms=response_times_sorted[int(count * 0.95)],
                p99_response_time_ms=response_times_sorted[int(count * 0.99)],
                error_count=len(errors),
                error_rate=len(errors) / len(endpoint_metrics),
                success_rate=1.0 - (len(errors) / len(endpoint_metrics)),
            )
            aggregates.append(agg)
        
        return {
            "timestamp": dt.datetime.now().isoformat(),
            "window_seconds": window_seconds,
            "total_checks": len(metrics),
            "aggregates": [a.to_dict() for a in aggregates],
        }
    
    async def save_report(self, report: dict, report_type: str = "periodic") -> None:
        """Save report to file"""
        report_dir = MONITOR_HOME / report_type / dt.datetime.now().strftime("%Y-%m-%d")
        report_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = dt.datetime.now().strftime("%H-%M-%S")
        report_file = report_dir / f"{timestamp}.json"
        
        with open(report_file, "w") as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Saved {report_type} report: {report_file}")
    
    async def monitor_loop(self) -> None:
        """Main monitoring loop"""
        logger.info(f"Starting KukuiBot monitor for {self.url}")
        logger.info(f"Check interval: {CHECK_INTERVAL}s, Report interval: {REPORT_INTERVAL}s")
        
        try:
            while True:
                # Run endpoint checks
                await self.run_check_cycle()
                
                # Check if we should generate a report
                now = time.time()
                if now - self.last_report_time >= REPORT_INTERVAL:
                    logger.info("Generating periodic report...")
                    report = await self.generate_report(window_seconds=REPORT_INTERVAL)
                    await self.save_report(report, "periodic")
                    self.last_report_time = now
                
                # Check if we should generate a daily report
                if now - self.last_daily_report_time >= DAILY_REPORT_INTERVAL:
                    logger.info("Generating daily report...")
                    report = await self.generate_report(window_seconds=DAILY_REPORT_INTERVAL)
                    await self.save_report(report, "daily")
                    self.last_daily_report_time = now
                
                # Wait before next check
                await asyncio.sleep(CHECK_INTERVAL)
        
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
        except Exception as e:
            logger.error(f"Monitor error: {e}", exc_info=True)
            raise


async def main():
    """Entry point"""
    monitor = Monitor()
    await monitor.monitor_loop()


if __name__ == "__main__":
    asyncio.run(main())
