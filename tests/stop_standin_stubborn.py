"""A stand-in worker that ignores SIGTERM, as a worker stuck in a long call might (#410). Only SIGKILL ends it."""
import os
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["STOP_STANDIN_LOG"], "a", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()} ready\n")
while True:
    time.sleep(1)
