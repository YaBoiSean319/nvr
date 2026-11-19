"""A supervisor over stand-in workers, run as its own process the way the container runs the real one, for the stop test (#410).
It writes "<name> <pid>" for each worker it starts to STOP_STANDIN_PIDS and exits with what Supervisor.run returns."""
import logging
import os
import sys

from app.store import initialize
from app.workers import supervisor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
initialize()
started = supervisor.Worker.start


def start(worker):
    started(worker)
    with open(os.environ["STOP_STANDIN_PIDS"], "a", encoding="utf-8") as handle:
        handle.write(f"{worker.name} {worker.process.pid}\n")


supervisor.Worker.start = start
watched = supervisor.Supervisor([
    supervisor.Worker("recorder", "tests.stop_standin"),
    supervisor.Worker("exporter", "tests.stop_standin_stubborn"),
    supervisor.Worker("ai", "tests.stop_standin_stubborn"),
    supervisor.Worker("web", "tests.stop_standin"),
])
supervisor.signal.signal(supervisor.signal.SIGTERM, watched.request_stop)
sys.exit(watched.run())
