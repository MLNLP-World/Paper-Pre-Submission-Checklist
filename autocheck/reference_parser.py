"""Conservative PDF bibliography segmentation; no network or source LaTeX required."""
from dataclasses import dataclass, field
from collections import Counter
import re
import unicodedata

from .reference_layout import extract_reference_sections

REFERENCES_HEADING_PATTERN = re.compile(r'^(?:\d+\.?\s+)?(?:References|Bibliography)$', re.IGNORECASE)
# An unbracketed four-digit year on a wrapped author/year header is not a
# reference label. Otherwise a damaged author line could leave a fake "entry".
NUMBERED_ENTRY_PATTERN = re.compile(r'^(?:\[\d+\]|(?!(?:18|19|20)\d{2}\.)\d+\.)\s*')
AUTHOR_YEAR_PATTERN = re.compile(
    r'^(?P<authors>.+?)(?:\.\s+(?:18|19|20)\d{2}[a-z]?\.'
    r'|\s+\((?:18|19|20)\d{2}[a-z]?\)\.?)(?=\s|$)'
)
AUTHOR_PARTICLES = {"and", "et", "al", "de", "del", "della", "da", "di", "dos", "das",
                    "du", "van", "von", "der", "den", "la", "le", "bin"}
MAX_AUTHOR_LINES = 12


def _join_reference_lines(lines):
    # 合并 PDF 断行，同时还原排版引入的单词断字（如 refer-\nence）。
    text = re.sub(r'(?<=\w)-\n(?=\w)', '', '\n'.join(lines))
    return _normalize_accents(re.sub(r'\s+', ' ', text).strip())


def _normalize_accents(text):
    # TeX PDFs may emit a spacing accent before its base letter (R´e, Nystr¨om).
    # Restrict recovery to letters; apostrophes and punctuation are left intact.
    marks = {'´': '\u0301', '¨': '\u0308', 'ˆ': '\u0302', 'ˇ': '\u030c', '¸': '\u0327'}
    text = re.sub(r'([´¨ˆˇ¸])([^\W\d_])',
                  lambda match: match[2] + marks[match[1]], text)
    return unicodedata.normalize('NFC', text)


def _looks_like_authors(text):
    # 作者姓名、首字母及常见姓名连接词；不把普通正文中的年份当成条目起点。
    text = _normalize_accents(text)
    text = re.sub(r'\s*\((?:eds?|editors?)\)\.?$', '', text, flags=re.IGNORECASE)
    if re.search(r'[\d_]|[^\w\s.,\-\'’&]', text):
        return False
    # 全名中常见的是单字母缩写，不能把上一条的完整句子拼进下一条作者列表。
    for match in re.finditer(r'\b([^\W\d_]{2,})\.(?=\s+\S)', text):
        if match.group(1).casefold() not in {'al', 'jr', 'sr'}:
            return False
    words = re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", text)
    return (bool(words) and any(word[0].isupper() for word in words)
            and all(word[0].isupper() or word in AUTHOR_PARTICLES for word in words))


def _author_year_header_end(lines, start):
    """找到作者—年份头的末行，支持作者列表及年份换行；无法识别时返回 None。"""
    for end in range(start, min(len(lines), start + MAX_AUTHOR_LINES)):
        prefix = _join_reference_lines(lines[start:end + 1])
        match = AUTHOR_YEAR_PATTERN.match(prefix)
        if match:
            return end if _looks_like_authors(match.group('authors')) else None
        if not _looks_like_authors(prefix):
            return None
        # 完整句子不能与下一条作者行合并；允许年份紧随作者句点换行。
        if lines[end].endswith('.') and not re.search(r'\b[A-Z]\.$', lines[end]):
            if end + 1 < len(lines):
                match = AUTHOR_YEAR_PATTERN.match(_join_reference_lines(lines[start:end + 2]))
                if match and _looks_like_authors(match.group('authors')):
                    return end + 1
            return None
    return None


def _year_end_author_prefix_end(text):
    """识别以句点结束的姓名列表，返回题名起点；不接受普通小写句子。"""
    for match in re.finditer(r'\.(?:\s+|$)', text):
        authors = text[:match.start()]
        words = re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", authors)
        if not words or not _looks_like_authors(authors):
            continue
        rest = text[match.end():]
        if re.match(r'\((?:eds?|editors?)\)\.', rest, re.IGNORECASE):
            continue
        # “Field, A. and Stone, B.” 中的首字母句点不是作者列表终点。
        if re.match(r'(?:and\b|et\b|&|,)', rest):
            continue
        if rest and not rest[0].isalpha():
            continue
        return match.end()
    return None


@dataclass
class UnresolvedReference:
    text: str
    page: int
    reason: str


@dataclass
class ReferenceParseResult:
    entries: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    found: bool = False
    entry_pages: list = field(default_factory=list)

    @property
    def complete(self):
        return self.found and bool(self.entries) and not self.unresolved


# A publication year is a field, not any year mentioned in a sentence. Allow
# publisher / DOI / URL / access-date fields after it, as real styles require.
PUBLICATION_YEAR = re.compile(r'(?:[,.]\s+|\()(?:18|19|20)\d{2}[a-z]?(?:[.)](?=\s|$)|$)')
TRAILING_FIELD = re.compile(
    r'^(?:https?://|www\.|doi\b|url\b|accessed\b|available\b|retrieved\b|'
    r'arxiv\b|in\b|proceedings\b|journal\b|transactions\b|volume\b|vol\.|'
    r'pages?\b|pp\.|[0-9]+[–:-])', re.IGNORECASE)


def _valid_entry(text):
    numbered = NUMBERED_ENTRY_PATTERN.match(text)
    if numbered:
        return any(char.isalpha() for char in text[numbered.end():])
    header = AUTHOR_YEAR_PATTERN.match(text)
    if header and _looks_like_authors(header['authors']):
        return any(char.isalpha() for char in text[header.end():])
    author_end = _year_end_author_prefix_end(text)
    if author_end is None:
        return False
    return any(year.start() > author_end and
               any(char.isalpha() for char in text[author_end:year.start()])
               for year in PUBLICATION_YEAR.finditer(text))


def _has_terminal_year(text):
    return bool(re.search(r'(?:[,.]\s+)(?:18|19|20)\d{2}[a-z]?\.?$', text))


def _column_layout(lines):
    """Infer hanging indents from repeated starts, relative to each column.

    A minority of offset lines is insufficient: it could be a wrapped URL or
    an isolated symbol. No conference-specific coordinate is assumed.
    """
    groups = {}
    for line in lines:
        groups.setdefault((line.page, line.column), []).append(line)
    layout = {}
    total_starts = total_indented = 0
    for key, group in groups.items():
        left = min(line.bbox[0] for line in group)
        positions = Counter(round((line.bbox[0] - left) / 2) * 2 for line in group)
        indented = sum(count for offset, count in positions.items() if 4 <= offset <= 24)
        total_starts += positions[0]
        total_indented += indented
        hanging = positions[0] >= 2 and indented >= 2
        layout[key] = (left, hanging)
    # Short first/last reference pages may have only one item, but still use
    # the same hanging indent as the well-populated pages of this section.
    has_hanging = (any(value[1] for value in layout.values())
                   or total_starts >= 2 and total_indented >= 2)
    if has_hanging:
        for key, group in groups.items():
            left, hanging = layout[key]
            if not hanging and any(4 <= line.bbox[0] - left <= 24 for line in group):
                layout[key] = (left, True)
    return layout


def _parse_section(section):
    result = ReferenceParseResult(found=True)
    lines = [line for line in section.lines
             if not REFERENCES_HEADING_PATTERN.fullmatch(line.text.strip())]
    if not lines:
        result.unresolved.append(UnresolvedReference('', section.heading.page, 'empty_section'))
        return result
    texts = [_normalize_accents(line.text) for line in lines]
    layout = _column_layout(lines)
    pending = []

    def flush():
        if not pending:
            return
        text = _join_reference_lines([texts[index] for index in pending])
        page = lines[pending[0]].page
        if _valid_entry(text):
            result.entries.append(text)
            result.entry_pages.append(page)
        else:
            result.unresolved.append(UnresolvedReference(text, page, 'unrecognized_entry'))
        pending.clear()

    for index, line in enumerate(lines):
        text = texts[index]
        left, hanging = layout[(line.page, line.column)]
        first_position = line.bbox[0] <= left + 2
        numbered = bool(NUMBERED_ENTRY_PATTERN.match(text))
        year_head = _author_year_header_end(texts, index) is not None
        # An unsupported author spelling still marks a possible new item. It
        # must become unresolved, rather than inheriting the previous header.
        possible_year_head = bool(AUTHOR_YEAR_PATTERN.match(text))
        if (text.endswith('.') and index + 1 < len(texts)
                and re.match(r'^(?:18|19|20)\d{2}[a-z]?\.', texts[index + 1])):
            possible_year_head = True
        author_start = _year_end_author_prefix_end(
            _join_reference_lines(texts[index:index + MAX_AUTHOR_LINES])) is not None
        previous_text = _join_reference_lines([texts[item] for item in pending])
        previous_complete = _valid_entry(previous_text)

        if pending:
            if hanging:
                # Entry starts share the outer edge; continuations are inset.
                # Across pages/columns an inset first line must stay attached.
                starts = first_position
            else:
                starts = (numbered or year_head or possible_year_head
                          or (previous_complete and author_start))
                previous_line = lines[pending[-1]]
                paragraph_gap = (line.page == previous_line.page
                                 and line.column == previous_line.column
                                 and line.bbox[1] - previous_line.bbox[1]
                                 > 1.8 * max(line.font_size, previous_line.font_size))
                if previous_complete and paragraph_gap:
                    starts = True
                # A complete year-end item followed by unexplained text is an
                # unresolved fragment, not an extension silently deemed valid.
                if (previous_complete and _has_terminal_year(previous_text)
                        and not TRAILING_FIELD.match(text)):
                    starts = True
                # Preserve a failed item instead of swallowing it into a later
                # recognisable author header.
                if not previous_complete and author_start:
                    if (not _looks_like_authors(previous_text)
                            or previous_text.endswith('.')
                            and not re.search(r'\b[A-Z]\.$', previous_text)):
                        starts = True
            if starts:
                # A publication year alone can resemble a numbered label;
                # keep it with an unfinished entry across a page/column break.
                if not (re.fullmatch(r'(?:18|19|20)\d{2}[a-z]?\.?', text)
                        and not previous_complete):
                    flush()
        pending.append(index)
    flush()
    if not section.end_known:
        result.unresolved.append(UnresolvedReference('', lines[-1].page, 'section_boundary'))
    return result


def _parse_reference_document(doc):
    sections = extract_reference_sections(doc)
    if not sections:
        return ReferenceParseResult()
    results = [_parse_section(section) for section in sections]
    # Prefer greatest coverage, preserving the earlier range on ties. Selecting
    # a cleaner suffix at a repeated heading would hide unresolved earlier items.
    return max(results, key=lambda result: len(result.entries))
