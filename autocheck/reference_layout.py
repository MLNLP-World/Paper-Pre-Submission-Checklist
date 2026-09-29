"""Recover bibliography reading order without discarding PDF layout evidence.

This module only locates and orders candidate reference sections. Whether an
individual paragraph is a reference remains the reference parser's decision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from statistics import median

from .pdf_text import get_review_number_bboxes


_REFERENCE_HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?(?:References|Bibliography)$", re.IGNORECASE
)
_EXPLICIT_APPENDIX = re.compile(
    r"^(?:(?:Appendix|Appendices|Supplementary Material)(?:\s|$)"
    r"|[A-Z](?:\.\d+)*\.?\s+(?:Appendix|Additional|Supplementary)\b)",
    re.IGNORECASE,
)
_LETTER_HEADING = re.compile(r"^[A-Z](?:\.\d+)*\.?\s+\S")
_YEAR = re.compile(r"^(?:18|19|20)\d{2}$")


@dataclass(frozen=True)
class ReferenceLine:
    text: str
    page: int
    bbox: tuple
    font_size: float
    column: int = 0
    raw_text: str = ""
    baseline: float | None = None


@dataclass
class ReferenceSection:
    lines: list[ReferenceLine]
    heading: ReferenceLine
    end_known: bool = True


def _body_lines(lines):
    return [line for line in lines
            if len(line.text) >= 20 and not line.text.isdigit()
            and line.bbox[2] - line.bbox[0] >= 80]


def _column_split(lines, width):
    """Find two sustained text columns, not hanging indents or short labels."""
    body = _body_lines(lines)
    if len(body) < 4:
        return None
    # A large gap between text starts identifies potential left/right columns.
    # Small within-column shifts include hanging indentation and PDF fragments.
    ordered = sorted(body, key=lambda line: line.bbox[0])
    candidates = []
    for index in range(2, len(ordered) - 1):
        gap = ordered[index].bbox[0] - ordered[index - 1].bbox[0]
        if gap < width * 0.20:
            continue
        left, right = ordered[:index], ordered[index:]
        right_start = median(line.bbox[0] for line in right)
        left_ends = sorted(line.bbox[2] for line in left)
        left_end = left_ends[int((len(left_ends) - 1) * 0.75)]
        if left_end >= right_start - 4:
            continue  # Wide text crossing the alleged gutter is one column.
        left_range = (min(line.bbox[1] for line in left), max(line.bbox[3] for line in left))
        right_range = (min(line.bbox[1] for line in right), max(line.bbox[3] for line in right))
        if min(left_range[1], right_range[1]) < max(left_range[0], right_range[0]):
            continue
        candidates.append((min(len(left), len(right)), (left_end + right_start) / 2))
    return max(candidates)[1] if candidates else None


def _merge_row_fragments(lines):
    """Join same-row fragments such as 'A' + an appendix title, within a column."""
    def baseline(line):
        return line.baseline if line.baseline is not None else line.bbox[3]

    rows = []
    for line in sorted(lines, key=lambda item: (baseline(item), item.bbox[0])):
        if (rows and abs(baseline(rows[-1][0]) - baseline(line))
                <= max(0.8, min(rows[-1][0].font_size, line.font_size) * 0.18)):
            rows[-1].append(line)
        else:
            rows.append([line])
    merged = []
    for row in rows:
        combined = []
        for line in sorted(row, key=lambda item: item.bbox[0]):
            if not combined:
                combined.append(line)
                continue
            previous = combined[-1]
            gap = line.bbox[0] - previous.bbox[2]
            if not -1 <= gap <= max(previous.font_size, line.font_size) * 2.2:
                combined.append(line)
                continue
            bbox = (min(previous.bbox[0], line.bbox[0]), min(previous.bbox[1], line.bbox[1]),
                    max(previous.bbox[2], line.bbox[2]), max(previous.bbox[3], line.bbox[3]))
            combined[-1] = replace(previous, text=previous.text + " " + line.text,
                                   raw_text=previous.raw_text + " " + line.raw_text,
                                   bbox=bbox, font_size=max(previous.font_size, line.font_size))
        merged.extend(combined)
    return merged


def _order_page(lines, width):
    split = _column_split(lines, width)
    if split is None:
        return _merge_row_fragments(lines)
    # Full-width material separates successive column regions. This also keeps
    # a wide heading above both columns rather than moving it into the left one.
    wide = [line for line in lines
            if line.bbox[0] < split - width * 0.15
            and line.bbox[2] > split + width * 0.15]
    ordinary = [line for line in lines if line not in wide]
    result = []
    for boundary in sorted(wide, key=lambda line: line.bbox[1]) + [None]:
        cutoff = boundary.bbox[1] if boundary is not None else float("inf")
        region = [line for line in ordinary if line.bbox[1] < cutoff]
        ordinary = [line for line in ordinary if line.bbox[1] >= cutoff]
        for column in (0, 1):
            selected = [replace(line, column=column) for line in region
                        if (line.bbox[0] >= split) == bool(column)]
            result.extend(_merge_row_fragments(selected))
        if boundary is not None:
            result.append(boundary)
    return result


def _likely_citation(text):
    # Edge position and repetition alone do not make a citation a running header.
    # Preserve numbered items, dates, URLs and author-name/punctuation patterns.
    if (re.match(r"^(?:\[\d+\]|\d+\.)\s*", text)
            or re.search(r"https?://", text) or _YEAR.fullmatch(text)):
        return True
    prefix = text.split(".", 1)[0]
    if re.search(r"\d", prefix):
        return False  # A year in a running conference header is not a citation.
    words = re.findall(r"[^\W\d_]+", prefix)
    particles = {"and", "de", "del", "van", "von", "der", "la", "le", "et", "al"}
    names = (len(words) >= 2 and all(word[0].isupper() or word in particles for word in words))
    return names and ("." in text or "," in text or " and " in text)


def _read_lines(doc):
    pages = []
    rulers = []
    edge_pages = {}
    edge_candidates = set()
    for page_number, page in enumerate(doc, 1):
        blocks = page.get_text("dict", flags=0)["blocks"]
        ignored = get_review_number_bboxes(page, blocks)
        lines = []
        for block in blocks:
            if block.get("type") != 0:
                continue
            for item in block.get("lines", []):
                raw = "".join(span["text"] for span in item["spans"])
                text = raw.strip()
                if not text:
                    continue
                bbox = tuple(item["bbox"])
                size = max((span["size"] for span in item["spans"]), default=0)
                if bbox in ignored:
                    rulers.append((page.rect.width, bbox[0], bbox[2], size))
                    continue
                edge = bbox[3] < page.rect.height * 0.075 or bbox[1] > page.rect.height * 0.90
                if (edge and text.isascii() and text.isdigit() and not _YEAR.fullmatch(text)
                        and (len(text) <= 3 or int(text) == page_number)):
                    continue
                baseline = median([span["origin"][1] for span in item["spans"]
                                   if "origin" in span] or [bbox[3]])
                line = ReferenceLine(text, page_number, bbox, size, raw_text=raw, baseline=baseline)
                lines.append(line)
                if edge and not _REFERENCE_HEADING.fullmatch(text) and not _likely_citation(text):
                    edge_pages.setdefault(text, set()).add(page_number)
                    edge_candidates.add((page_number, bbox))
        pages.append((page.rect.width, lines))

    result = []
    for width, lines in pages:
        body = _body_lines(lines)
        left = min((line.bbox[0] for line in body), default=0)
        right = max((line.bbox[2] for line in body), default=width)
        cleaned = []
        for line in lines:
            if ((line.page, line.bbox) in edge_candidates
                    and len(edge_pages.get(line.text, ())) > 1):
                continue
            # A ruler may have only one visible number on the last page. Reuse
            # proven margin positions, but only outside this page's text extent.
            outside = line.bbox[2] < left - 4 or line.bbox[0] > right + 4
            if (outside and line.text.isascii() and line.text.isdigit()
                    and any(abs(width - ruler_width) < 1
                            and (abs(line.bbox[0] - x0) < 2 or abs(line.bbox[2] - x1) < 2)
                            and abs(line.font_size - size) < 0.5
                            for ruler_width, x0, x1, size in rulers)):
                continue
            cleaned.append(line)
        result.extend(_order_page(cleaned, width))
    return result


def _ends_section(line, following, heading, body_size):
    if _EXPLICIT_APPENDIX.match(line.text):
        return True
    # Some extractors separate the appendix letter and title into different rows.
    if (re.fullmatch(r"[A-Z]", line.text) and following is not None
            and following.page == line.page and following.column == line.column
            and following.bbox[1] - line.bbox[3] <= max(line.font_size, following.font_size) * 2
            and re.search(r"\b(?:Appendix|Supplementary)\b", following.text, re.IGNORECASE)):
        return True
    # A chapter letter alone is insufficient: initials in names look the same.
    # Require heading typography relative to the bibliography's actual text.
    if _LETTER_HEADING.match(line.text):
        return (line.font_size >= heading.font_size * 0.93
                and line.font_size > body_size * 1.08)
    return False


def extract_reference_sections(doc):
    """Return candidate sections with page and position information intact.

    Later headings are also candidates: a template may discuss a section named
    References before its actual bibliography. Repeated headings are retained
    in a candidate's lines so the parser can distinguish an interrupted entry
    from an unrelated earlier section instead of silently joining the two.
    """
    lines = _read_lines(doc)
    sections = []
    for index, heading in enumerate(lines):
        if not _REFERENCE_HEADING.fullmatch(heading.text):
            continue
        following = lines[index + 1:]
        body_size = median([line.font_size for line in following[:30]
                            if len(line.text) >= 20] or [heading.font_size])
        content = []
        for offset, line in enumerate(following):
            next_line = following[offset + 1] if offset + 1 < len(following) else None
            if _ends_section(line, next_line, heading, body_size):
                break
            content.append(line)
        sections.append(ReferenceSection(content, heading))
    return sections
