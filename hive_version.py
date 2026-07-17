"""Single source of truth for the swarm worker code version.

Bump ``CODE_VERSION`` by 1 whenever you change any *downloadable* worker asset
(``worker_loop.py``, ``hive_caps.py``, ``hive_queue.py``, ``hive_log.py``,
``hive_version.py``) and want the fleet to pick it up. The control plane
advertises this number on every ``/claim`` response header
(``X-Hive-Code-Version``) and via ``GET /version``; each worker compares it to
its own compiled-in copy and self-updates when:

  * mode A (command) — an operator runs ``halctl update --worker <lane>`` (or
    ``--all``), which sets ``workers.update_to``. The worker acts on the
    ``X-Hive-Update-To`` header on its next idle claim. Canary-able.
  * mode B (auto)    — the worker has ``HIVE_AUTO_UPDATE=1`` in its env and the
    advertised version is *newer* than its own. Forward-only, whole-fleet.

Self-update is worker-native daemon code (no copilot/LLM in the loop), so it is
NOT subject to the agent safety model that blocks in-band task-driven upgrades.
"""

CODE_VERSION = 10
