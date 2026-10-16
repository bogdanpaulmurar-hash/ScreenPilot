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
    count = 0
    for case in root.iter("testcase"):
        for problem in list(case.findall("failure")) + list(case.findall("error")):
            if count >= MAX_ANNOTATIONS:
                return 0
            title = f"{case.get('classname')}.{case.get('name')}"
            body = (problem.get("message") or "") + "\n" + (problem.text or "")
            print(f"::error title={escape(title)}::{escape(body[-MAX_CHARS:])}")
            count += 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "junit.xml"))
