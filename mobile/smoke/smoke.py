"""Emulator smoke test for the Android app (#140): install it, give it the NVR's address the way the start screen would keep it, open it, and wait for the NVR's own page.

    python3 smoke.py app-debug.apk http://10.0.2.2:8765 "Create the administrator account"

Needs ``adb`` with one emulator or device attached.
"""
from __future__ import annotations

import subprocess
import sys
import time
import xml.etree.ElementTree as ET

PACKAGE = "app.nvr.mobile"
ACTIVITY = f"{PACKAGE}/.MainActivity"


def adb(*arguments: str, check: bool = True, timeout: int = 120) -> str:
    result = subprocess.run(["adb", *arguments], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise SystemExit(f"adb {' '.join(arguments)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def screen_text() -> str:
    adb("shell", "uiautomator", "dump", "/sdcard/window.xml", check=False)
    dump = adb("shell", "cat", "/sdcard/window.xml", check=False)
    try:
        root = ET.fromstring(dump[dump.index("<?xml"):])
    except (ValueError, ET.ParseError):
        return ""
    return "\n".join(filter(None, (f"{node.get('text', '')} {node.get('content-desc', '')}".strip() for node in root.iter("node"))))


def screenshot(name: str) -> None:
    with open(name, "wb") as handle:
        handle.write(subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, timeout=60).stdout)


def main(apk: str, address: str, expected: str) -> int:
    adb("wait-for-device")
    for _ in range(90):
        if adb("shell", "getprop", "sys.boot_completed", check=False).strip() == "1":
            break
        time.sleep(2)
    adb("install", "-r", apk)
    # The start screen keeps the address in the app's preferences; write it there as it would, so the test does not depend on typing into a web view.
    adb("shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(5)
    adb("shell", "am", "force-stop", PACKAGE)
    with open("CapacitorStorage.xml", "w") as handle:
        handle.write(f'<?xml version="1.0" encoding="utf-8" standalone="yes" ?><map><string name="server.url">{address}</string></map>')
    adb("push", "CapacitorStorage.xml", "/data/local/tmp/CapacitorStorage.xml")
    adb("shell", f"run-as {PACKAGE} sh -c 'mkdir -p shared_prefs && cp /data/local/tmp/CapacitorStorage.xml shared_prefs/CapacitorStorage.xml'")
    adb("shell", "am", "start", "-n", ACTIVITY)
    deadline = time.time() + 150
    text = ""
    while time.time() < deadline:
        text = screen_text()
        if expected in text:
            screenshot("smoke-ok.png")
            print(f"The app opened the NVR: found {expected!r} on screen.")
            break
        time.sleep(3)
    else:
        screenshot("smoke-failed.png")
        print("Screen text at the end:\n" + text)
        return 1
    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    raise SystemExit(main(*sys.argv[1:]))
