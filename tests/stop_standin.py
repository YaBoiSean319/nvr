"""A stand-in worker for the supervisor's stop tests (#410). On SIGTERM it takes STOP_STANDIN_FINISH_SECONDS to finish, as the recorder
takes a moment to close and index its segments, then exits. Each step is written to STOP_STANDIN_LOG as "<pid> <event>"."""
import os
import signal
import time

log = os.environ["STOP_STANDIN_LOG"]


def note(event: str) -> None:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {event}\n")


asked = []
signal.signal(signal.SIGTERM, lambda *_: asked.append(True))
note("ready")
while not asked:
    time.sleep(0.02)
note("asked")
time.sleep(float(os.environ.get("STOP_STANDIN_FINISH_SECONDS", "0")))
note("finished")
