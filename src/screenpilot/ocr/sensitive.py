"""Find sensitive data in OCR results so it can be blurred automatically.

Detected: e-mail addresses, IBANs (checksum-validated), payment card numbers
(Luhn-validated), Romanian personal numeric codes (CNP, checksum-validated),
phone numbers and IPv4 addresses. Matching runs on each OCR line and maps
back to the word rectangles that contain the match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from screenpilot.capture.geometry import Rect
from screenpilot.ocr.engine import OcrLine, OcrResult

KINDS = ("email", "iban", "card", "cnp", "phone", "ip")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_CNP = re.compile(r"\b[1-9]\d{12}\b")
_PHONE = re.compile(r"(?:(?<=\s)|^)(?:\+|00)?\d{1,3}?[ .-]?\(?\d{2,4}\)?(?:[ .-]?\d{2,4}){2,4}\b")
_IP = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


@dataclass(frozen=True)
class SensitiveMatch:
    kind: str
    text: str
    rect: Rect


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def iban_ok(value: str) -> bool:
    s = value.replace(" ", "").upper()
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    number = "".join(str(int(c, 36)) for c in moved)
    return int(number) % 97 == 1


def cnp_ok(value: str) -> bool:
    if len(value) != 13 or not value.isdigit():
        return False
    key = "279146358279"
    total = sum(int(a) * int(b) for a, b in zip(value[:12], key))
    control = total % 11
    control = 1 if control == 10 else control
    return control == int(value[12])


def _candidates(text: str) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    for m in _EMAIL.finditer(text):
        found.append(("email", m.start(), m.end()))
    for m in _IBAN.finditer(text.upper()):
        if iban_ok(m.group()):
            found.append(("iban", m.start(), m.end()))
    for m in _CNP.finditer(text):
        if cnp_ok(m.group()):
            found.append(("cnp", m.start(), m.end()))
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits) and not cnp_ok(digits):
            found.append(("card", m.start(), m.end()))
    for m in _IP.finditer(text):
        found.append(("ip", m.start(), m.end()))
    for m in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 9 <= len(digits) <= 15:
            found.append(("phone", m.start(), m.end()))
    # drop matches contained in an earlier, more specific one
    found.sort(key=lambda f: (f[1], -(f[2] - f[1])))
    result: list[tuple[str, int, int]] = []
    for kind, start, end in found:
        if any(start >= s and end <= e for _, s, e in result):
            continue
        result.append((kind, start, end))
    return result


def _line_matches(line: OcrLine, kinds: set[str]) -> list[SensitiveMatch]:
    text = ""
    spans = []
    for word in line.words:
        if text:
            text += " "
        start = len(text)
        text += word.text
        spans.append((start, len(text), word))
    matches = []
    for kind, start, end in _candidates(text):
        if kind not in kinds:
            continue
        rect = Rect(0, 0, 0, 0)
        for s, e, word in spans:
            if s < end and e > start:
                rect = rect.union(word.rect)
        if not rect.is_empty:
            matches.append(SensitiveMatch(kind, text[start:end], rect))
    return matches


def find_sensitive(result: OcrResult, kinds=KINDS) -> list[SensitiveMatch]:
    wanted = set(kinds)
    matches: list[SensitiveMatch] = []
    for line in result.lines:
        matches.extend(_line_matches(line, wanted))
    return matches


def blur_rects(matches: list[SensitiveMatch], pad: int = 3) -> list[Rect]:
    return [Rect(m.rect.x - pad, m.rect.y - pad, m.rect.width + 2 * pad, m.rect.height + 2 * pad) for m in matches]
