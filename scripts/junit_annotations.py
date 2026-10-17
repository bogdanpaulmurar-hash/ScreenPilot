"""Turn pytest JUnit XML failures into GitHub Actions error annotations.

Annotations are visible in the PR / commit UI and through the Checks API,
which makes failures readable without downloading full job logs.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

MAX_ANNOTATIONS = 10
MAX_CHARS = 3000


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: str) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::warning::No JUnit report ({exc})")
        return 0
    cases = list(root.iter("testcase"))
    skipped: dict[str, int] = {}
    for case in cases:
        if case.find("skipped") is not None:
            module = (case.get("classname") or "?").split(".")[-2]
            skipped[module] = skipped.get(module, 0) + 1
    summary = ", ".join(f"{m}={n}" for m, n in sorted(skipped.items())) or "none"
    print(f"::notice title=Test summary::{len(cases)} tests, skipped: {summary}")
    count = 0
    for case in cases:
        for problem in list(case.findall("failure")) + list(case.findall("error")):
            if count >= MAX_ANNOTATIONS:
                return 0
            title = f"{case.get('classname')}.{case.get('name')}"
            body = (problem.get("message") or "") + "\n" + (problem.text or "")
            print(f"::error title={escape(title)}::{escape(body[-MAX_CHARS:])}")
            count += 1
    return 0


def log_tail(path: str, limit: int = 3500) -> None:
    """Print the end of the test output (crashes leave no JUnit failure entry)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()[-limit:]
    except OSError:
        return
    if " failed" not in text and "error" not in text.lower() and "Fatal" not in text:
        return
    print(f"::warning title=pytest output (tail)::{escape(text)}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    code = main(sys.argv[1] if len(sys.argv) > 1 else "junit.xml")
    if len(sys.argv) > 2:
        log_tail(sys.argv[2])
    sys.exit(code)
