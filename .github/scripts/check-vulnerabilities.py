"""Fail on a known vulnerability in the locked Python packages or the web interface's packages (#385).

Python: pip-audit over requirements.txt, the hashed lock the image installs (run it with the pip-audit from requirements-ci-audit.txt).
Web: npm audit over web/package-lock.json. A finding listed in .github/vulnerability-exceptions.txt, with a reason, is reported but
accepted. Prints what it found; exits 1 if anything not accepted is left, or if a check could not run."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXCEPTIONS = ROOT / ".github" / "vulnerability-exceptions.txt"
LINE = re.compile(r"^(?P<id>[A-Za-z]+-[\w-]+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<reason>\S.*)$")


def accepted() -> dict[str, str]:
    found: dict[str, str] = {}
    for number, raw in enumerate(EXCEPTIONS.read_text(encoding="utf-8").splitlines(), 1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        match = LINE.match(text)
        if not match:
            raise SystemExit(f"{EXCEPTIONS.name}:{number}: give the advisory ID, the date and a reason: {text!r}")
        found[match["id"].upper()] = match["reason"]
    return found


def python_findings(pip_audit: str) -> list[tuple[str, set[str], str]]:
    result = subprocess.run([pip_audit, "-r", str(ROOT / "requirements.txt"), "--disable-pip", "--no-deps", "--progress-spinner", "off", "-f", "json"],
                            capture_output=True, text=True, timeout=600)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"pip-audit did not run:\n{result.stderr[-2000:]}") from None
    return [(f"{package['name']} {package['version']}", {vuln["id"].upper(), *(alias.upper() for alias in vuln.get("aliases", []))}, vuln.get("description", "")[:160])
            for package in report.get("dependencies", []) for vuln in package.get("vulns", [])]


def web_findings() -> list[tuple[str, set[str], str]]:
    result = subprocess.run(["npm", "audit", "--json"], cwd=ROOT / "web", capture_output=True, text=True, timeout=300)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"npm audit did not run:\n{result.stderr[-2000:]}") from None
    if "error" in report:
        raise SystemExit(f"npm audit did not run: {report['error']}")
    found = []
    for name, entry in report.get("vulnerabilities", {}).items():
        for via in entry.get("via", []):
            if isinstance(via, dict):  # A string names another package that is itself listed
                advisory = str(via.get("url", "")).rsplit("/", 1)[-1].upper()
                found.append((f"{name} {entry.get('range', '')}".strip(), {advisory} if advisory else set(), str(via.get("title", ""))[:160]))
    return found


def main() -> int:
    pip_audit = sys.argv[1] if len(sys.argv) > 1 else "pip-audit"
    allowed = accepted()
    findings = [("python", *item) for item in python_findings(pip_audit)] + [("web", *item) for item in web_findings()]
    left = 0
    for where, package, ids, title in findings:
        reason = next((allowed[item] for item in ids if item in allowed), None)
        label = ", ".join(sorted(ids)) or "no advisory ID"
        if reason:
            print(f"accepted  {where}: {package}: {label}: {reason}")
        else:
            left += 1
            print(f"VULNERABLE {where}: {package}: {label}: {title}")
    print(f"{len(findings)} known vulnerabilities, {left} not accepted.")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
