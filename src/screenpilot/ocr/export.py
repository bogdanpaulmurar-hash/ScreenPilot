"""OCR text exports: plain text and Markdown (paragraphs and simple tables)."""

from __future__ import annotations

from screenpilot.ocr.engine import OcrLine, OcrResult

PARAGRAPH_GAP = 0.9  # vertical gap (in line heights) that starts a new paragraph
COLUMN_GAP = 2.5  # horizontal gap (in average char widths) that separates table cells


def to_plain_text(result: OcrResult) -> str:
    return result.text


def _cells(line: OcrLine) -> list[str]:
    words = sorted(line.words, key=lambda w: w.rect.x)
    if not words:
        return []
    char_w = sum(w.rect.width / max(1, len(w.text)) for w in words) / len(words)
    cells, current = [], [words[0].text]
    for prev, word in zip(words, words[1:]):
        if word.rect.x - prev.rect.right > COLUMN_GAP * char_w:
            cells.append(" ".join(current))
            current = []
        current.append(word.text)
    cells.append(" ".join(current))
    return cells


def to_markdown(result: OcrResult) -> str:
    """Paragraphs from vertical gaps; runs of lines with the same number of
    widely spaced columns (>= 2) become a Markdown table."""
    lines = sorted(result.lines, key=lambda l: (l.rect.y, l.rect.x))
    if not lines:
        return ""
    blocks: list[list[OcrLine]] = [[lines[0]]]
    for prev, line in zip(lines, lines[1:]):
        gap = line.rect.y - prev.rect.bottom
        if gap > PARAGRAPH_GAP * max(prev.rect.height, 1):
            blocks.append([])
        blocks[-1].append(line)
    out: list[str] = []
    for block in blocks:
        rows = [_cells(l) for l in block]
        if len(rows) >= 2 and all(len(r) >= 2 for r in rows) and len({len(r) for r in rows}) == 1:
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("|" + "---|" * len(rows[0]))
            out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
        else:
            out.append(" ".join(l.text for l in block))
        out.append("")
    return "\n".join(out).strip() + "\n"
