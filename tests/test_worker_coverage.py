"""Every background manager the application starts in-process is also started by exactly one supervised worker.

The application starts its managers itself in tests and development, and under the supervisor leaves them to the worker processes: a manager that no worker
starts runs only where nobody runs a production NVR (its digests are never sent, its events never read)."""
from __future__ import annotations

import importlib
import unittest

from app.application import STARTUP_ORDER

WORKERS = ("recorder", "exporter", "ai", "rules", "background")


def _service_class(module):
    return next(value for name, value in vars(module).items() if name.endswith("Service") and isinstance(value, type) and value.__module__ == module.__name__)


class WorkerCoverageTests(unittest.TestCase):
    def test_every_manager_is_started_by_a_worker(self):
        started: dict[type, list[str]] = {}
        for worker in WORKERS:
            service = _service_class(importlib.import_module(f"app.workers.{worker}"))()
            for value in vars(service).values():
                started.setdefault(type(value), []).append(worker)
        missing = [name for name, factory in STARTUP_ORDER if factory not in started]
        self.assertEqual(missing, [], "Managers the application starts in-process that no supervised worker starts")
        twice = {factory.__name__: workers for factory, workers in started.items() if len(workers) > 1 and factory in dict((f, n) for n, f in STARTUP_ORDER)}
        self.assertEqual(twice, {}, "Managers started by more than one worker")


if __name__ == "__main__":
    unittest.main()
