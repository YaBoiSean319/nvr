"""A stand-in worker for the supervisor's process-group tests: it starts a long-running child like the recorder starts FFmpeg, then idles."""
import os
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
with open(os.environ["GROUP_SPAWNER_CHILD"], "w", encoding="utf-8") as handle:
    handle.write(str(child.pid))
time.sleep(300)
