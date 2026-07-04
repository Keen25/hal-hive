#!/usr/bin/env python3
"""
HAL — Hive logging + metrics (issue #19 / U5)

A tiny, dependency-free observability layer shared by the queue core, the REST
server, and the worker. Replaces scattered ``print()`` calls with leveled
``logging`` and exposes a thread-safe in-process counter registry that the
server surfaces at ``GET /metrics``.

Environment:
  HIVE_LOG_LEVEL  DEBUG|INFO|WARNING|ERROR   (default INFO)
  HIVE_LOG_JSON   1 to emit one JSON object per line (structured log shipping);
                  anything else uses a human-readable single-line format.

Kept stdlib-only on purpose: workers self-install with no pip step, so the log
layer must not add a dependency.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading

_configured = False
_config_lock = threading.Lock()


class _JsonFormatter(logging.Formatter):
    """One JSON object per line so a shipper (vector/promtail/…) can parse the
    fleet's logs without a regex. Only stdlib json."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'ts': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        if record.exc_info:
            payload['exc'] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _configure_root() -> None:
    """Attach one stream handler to the root logger exactly once. Idempotent and
    safe to call from every module at import time."""
    global _configured
    if _configured:
        return
    with _config_lock:
        if _configured:
            return
        level_name = os.environ.get('HIVE_LOG_LEVEL', 'INFO').upper()
        level = getattr(logging, level_name, logging.INFO)
        handler = logging.StreamHandler(sys.stderr)
        if os.environ.get('HIVE_LOG_JSON') == '1':
            handler.setFormatter(_JsonFormatter())
        else:
            handler.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)-7s %(name)s: %(message)s',
                datefmt='%H:%M:%S',
            ))
        root = logging.getLogger()
        # Avoid duplicate handlers if a host framework already configured one.
        if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
            root.addHandler(handler)
        root.setLevel(level)
        _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger. Call this instead of ``print()``."""
    _configure_root()
    return logging.getLogger(name)


# ── Metrics: a thread-safe counter registry ───────────────────────────────────
#
# The reaper, merge loop, and request handlers all run in the same server
# process (separate threads), so a simple locked dict is enough — no shared
# memory, no external metrics backend. The server exposes snapshot() at /metrics.

_metrics: dict[str, int] = {}
_metrics_lock = threading.Lock()

# Declared up-front so /metrics always reports every counter (as 0) even before
# the first event — a missing key reads as "instrument broken", 0 reads as "idle".
_KNOWN_COUNTERS = (
    'claims',          # tasks handed to a worker
    'completes',       # /complete calls received
    'merges',          # branches fast-forwarded into the base ref
    'parks',           # tasks parked needs_attention (verify/merge conflict)
    'dead_letters',    # tasks that exhausted max_attempts
    'reaped',          # leases reclaimed by the reaper
    'retries',         # failed tasks re-queued with backoff
)


def incr(name: str, n: int = 1) -> None:
    """Increment a named counter by n (default 1). Thread-safe."""
    with _metrics_lock:
        _metrics[name] = _metrics.get(name, 0) + n


def snapshot() -> dict[str, int]:
    """Return a copy of all counters, with every known counter present (0 if
    never incremented)."""
    with _metrics_lock:
        snap = {k: 0 for k in _KNOWN_COUNTERS}
        snap.update(_metrics)
        return snap
