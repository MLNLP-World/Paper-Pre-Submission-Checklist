import re
from .core import LogLevel, CheckType, T
from .pdf_text import get_review_number_bboxes

UNRESOLVED_REF_PATTERN = re.compile(
    r'\[\?\]|\?\?|\b(?:Figure|Fig\.|Table|Equation|Eq\.|Section|Chapter)\s+\?',
    re.IGNORECASE,
)
DRAFT_MARKER_PATTERN = re.compile(r'\b(TODO|FIXME|XXX|TBD)\b', re.IGNORECASE)
REPEATED_WORD_PATTERN = re.compile(r'\b([A-Za-z]{2,})\s+\1\b', re.IGNORECASE)
FIGURE_NUM_PATTERN = re.compile(r'\b(?:Figure|Fig\.?)\s*(\d+)\b')
TABLE_NUM_PATTERN = re.compile(r'\bTable\s*(\d+)\b')
# 小写的图表编号引用（如 "figure 3"、"table 2"），论文中应大写为 Figure/Table
LOWERCASE_FIGTAB_PATTERN = re.compile(r'\b(?:figure|fig\.?|table)\s*\d+\b')
# 连续重复的标点（如 ,, ;; 以及非省略号、非数字范围的 ..）
STUTTERED_PUNCT_PATTERN = re.compile(r',,|;;|(?<![.\d])\.{2}(?![.\d])')
# 前后均为空白的孤立标点（如 "the , book"）；不含冒号，避免数学公式 "f : X" 误报
ISOLATED_PUNCT_PATTERN = re.compile(r'(?<!\S)[,;](?!\S)')
MAX_GAP_PREVIEW = 20
MAX_NUMBER_DIGITS = 9


def _content_text(page):
    """保留 PDF 阅读顺序，去掉几何上可确认的审稿行号并还原排版断字。"""
    raw_text = page.get_text()
    if not raw_text.strip():
        return raw_text
    try:
        structured = page.get_text('dict', flags=0)
    except (AttributeError, TypeError):
        structured = None  # 兼容只提供文本接口的调用方。
    if isinstance(structured, dict):
        blocks = structured.get('blocks', [])
        review_numbers = get_review_number_bboxes(page, blocks)
        lines = []
        for block in blocks:
            for line in block.get('lines', []):
                if tuple(line['bbox']) not in review_numbers:
                    lines.append(''.join(span['text'] for span in line['spans']))
        text = '\n'.join(lines)
    else:
        text = raw_text
    # Ta-\nble 等排版断词；不删除普通行内连字符，也不删除独立的正文数字。
    return re.sub(r'(?<=[A-Za-z])[-\u00ad][ \t]*\n\s*(?=[a-z])', '', text)


def _context_snippet(text, match, radius=20):
    """提取匹配位置前后的上下文片段，压缩空白便于在报告中定位。"""
    return " ".join(text[max(0, match.start() - radius):match.end() + radius].split())


def _find_number_gaps(numbers):
    """按相邻已出现编号计算缺号总数，只展开有限预览，不遍历整个数值范围。"""
    ordered = sorted(numbers)
    preview = []
    total = 0
    for left, right in zip(ordered, ordered[1:]):
        count = right - left - 1
        if count > 0:
            total += count
            preview.extend(range(left + 1, left + 1 + min(count, MAX_GAP_PREVIEW - len(preview))))
    return preview, total


class ContentCheck:
    def execute(self, pdf_path, doc, logger):
        unresolved_refs = set()
        draft_markers = []
        repeated_words = []
        abnormal_puncts = []
        lowercase_refs = []
        figure_nums = set()
        table_nums = set()
        full_text_parts = []
        unreadable_pages = []
        oversized_number_pages = []

        for page_num in range(doc.page_count):
            text = _content_text(doc[page_num])
            full_text_parts.append(text)

            # 没有文字可匹配不等于检查通过；不在此判断页面是否为空白或违规。
            if not text.strip():
                unreadable_pages.append(str(page_num + 1))
                continue

            # 检查未解析的 LaTeX 引用
            if UNRESOLVED_REF_PATTERN.search(text):
                unresolved_refs.add(str(page_num + 1))

            # 检查草稿残留标记，带上下文便于定位
            for match in DRAFT_MARKER_PATTERN.finditer(text):
                draft_markers.append(T(f"第 {page_num + 1} 页 \"{_context_snippet(text, match)}\"",
                                       f"page {page_num + 1} \"{_context_snippet(text, match)}\""))

            # 检查连续重复单词（如 "the the"），带上下文便于定位
            for match in REPEATED_WORD_PATTERN.finditer(text):
                repeated_words.append(T(f"第 {page_num + 1} 页 \"{_context_snippet(text, match)}\"",
                                        f"page {page_num + 1} \"{_context_snippet(text, match)}\""))

            # 检查异常标点（连续重复标点、孤立标点），带上下文便于定位
            for pattern in (STUTTERED_PUNCT_PATTERN, ISOLATED_PUNCT_PATTERN):
                for match in pattern.finditer(text):
                    abnormal_puncts.append(T(f"第 {page_num + 1} 页 \"{_context_snippet(text, match)}\"",
                                             f"page {page_num + 1} \"{_context_snippet(text, match)}\""))

            # 检查小写的图表编号引用（应为 Figure/Table）
            for match in LOWERCASE_FIGTAB_PATTERN.finditer(text):
                lowercase_refs.append(T(f"第 {page_num + 1} 页 \"{match.group(0)}\"",
                                        f"page {page_num + 1} \"{match.group(0)}\""))

            # 收集图表编号用于跳号检查
            oversized_number = False
            for pattern, numbers in ((FIGURE_NUM_PATTERN, figure_nums), (TABLE_NUM_PATTERN, table_nums)):
                for match in pattern.finditer(text):
                    number = match.group(1)
                    if len(number) > MAX_NUMBER_DIGITS:
                        oversized_number = True
                    else:
                        numbers.add(int(number))
            if oversized_number:
                oversized_number_pages.append(str(page_num + 1))

        if oversized_number_pages:
            pages = ', '.join(oversized_number_pages)
            logger.add(LogLevel.UNCHECKED, CheckType.CONTENT,
                       T(f"第 {pages} 页存在超过 {MAX_NUMBER_DIGITS} 位的图表编号，已跳过这些异常编号；编号连贯性检查不完整，请人工复核。",
                         f"Figure/Table numbers longer than {MAX_NUMBER_DIGITS} digits on page(s) {pages} were skipped; numbering check incomplete, please review manually."))

        if unreadable_pages:
            pages = ', '.join(unreadable_pages)
            logger.add(LogLevel.UNCHECKED, CheckType.CONTENT,
                       T(f"第 {pages} 页未提取到可用文字；这些页面基于文字的内容、匿名信息及引用检查无法完成。"
                         "本工具不识别图片中的文字，请人工复核。",
                         f"No extractable text on page(s) {pages}. Text-based content, anonymity and reference checks "
                         "could not be completed for these pages. This tool does not recognize text in images; "
                         "please review manually."))

        if unresolved_refs:
            pages = ', '.join(sorted(unresolved_refs, key=int))
            logger.add(LogLevel.ERROR, CheckType.CONTENT,
                       T(f"发现未解析的引用符号 ([?]/??/Figure ?等), 位于第 {pages} 页。",
                         f"Unresolved reference(s) ([?]/??/Figure ? etc.) found on page(s) {pages}."))

        if draft_markers:
            preview = ", ".join(draft_markers[:5]) + (" ..." if len(draft_markers) > 5 else "")
            logger.add(LogLevel.ERROR, CheckType.CONTENT,
                       T(f"发现 {len(draft_markers)} 处草稿标记 (TODO/FIXME等): {preview}",
                         f"Found {len(draft_markers)} draft marker(s) (TODO/FIXME etc.): {preview}"))

        if repeated_words:
            preview = ", ".join(repeated_words[:5]) + (" ..." if len(repeated_words) > 5 else "")
            logger.add(LogLevel.WARN, CheckType.CONTENT,
                       T(f"发现 {len(repeated_words)} 处连续重复单词: {preview}",
                         f"Found {len(repeated_words)} repeated-word occurrence(s): {preview}"))

        if abnormal_puncts:
            preview = ", ".join(abnormal_puncts[:5]) + (" ..." if len(abnormal_puncts) > 5 else "")
            logger.add(LogLevel.WARN, CheckType.CONTENT,
                       T(f"发现 {len(abnormal_puncts)} 处疑似异常标点（连续重复或孤立标点）: {preview}，请人工复核。",
                         f"Found {len(abnormal_puncts)} suspicious punctuation occurrence(s) (stuttered or isolated): "
                         f"{preview}; please verify manually."))

        if lowercase_refs:
            preview = ", ".join(lowercase_refs[:5]) + (" ..." if len(lowercase_refs) > 5 else "")
            logger.add(LogLevel.WARN, CheckType.CONTENT,
                       T(f"发现 {len(lowercase_refs)} 处图表引用首字母未大写（应为 Figure/Table）: {preview}",
                         f"Found {len(lowercase_refs)} figure/table reference(s) not capitalized "
                         f"(should be Figure/Table): {preview}"))

        # 检查明显未闭合的括号（简单栈匹配定位并给出上下文，可能存在误报）
        full_text = "\n".join(full_text_parts)
        page_starts = []
        offset = 0
        for part in full_text_parts:
            page_starts.append(offset)
            offset += len(part) + 1  # 页面之间以换行符连接

        def _page_of(pos):
            lo = 0
            while lo + 1 < len(page_starts) and page_starts[lo + 1] <= pos:
                lo += 1
            return lo + 1

        imbalances = []
        bracket_contexts = []
        for open_char, close_char in (("(", ")"), ("[", "]"), ("{", "}")):
            stack = []
            stray_closers = []
            for pos, char in enumerate(full_text):
                if char == open_char:
                    stack.append(pos)
                elif char == close_char:
                    if stack:
                        stack.pop()
                    else:
                        stray_closers.append(pos)
            diff = len(stack) - len(stray_closers)
            if diff != 0:
                imbalances.append(f"'{open_char}{close_char}' {diff:+d}")
            # 各取几处代表位置给出页码与上下文，便于人工定位
            for pos in stack[-3:]:
                page = _page_of(pos)
                snippet = " ".join(full_text[max(0, pos - 20):pos + 21].split())
                bracket_contexts.append(T(f"第 {page} 页 \"{snippet}\" 附近 '{open_char}' 未闭合",
                                          f"page {page} \"{snippet}\": '{open_char}' not closed"))
            for pos in stray_closers[:3]:
                page = _page_of(pos)
                snippet = " ".join(full_text[max(0, pos - 20):pos + 21].split())
                bracket_contexts.append(T(f"第 {page} 页 \"{snippet}\" 附近 '{close_char}' 没有匹配的开括号",
                                          f"page {page} \"{snippet}\": '{close_char}' has no matching opener"))
        if imbalances:
            preview = "; ".join(bracket_contexts[:5]) + (" ..." if len(bracket_contexts) > 5 else "")
            logger.add(LogLevel.WARN, CheckType.CONTENT,
                       T(f"检测到括号数量不平衡 ({', '.join(imbalances)})，可能存在未闭合的括号: {preview}，请人工复核。",
                         f"Unbalanced brackets detected ({', '.join(imbalances)}); possible unclosed brackets: {preview}; please verify manually."))

        # 检查图表序号连贯性（跳号）
        gap_msgs = []
        for label, numbers in (("Figure", figure_nums), ("Table", table_nums)):
            gaps, total = _find_number_gaps(numbers)
            if total:
                preview = ', '.join(map(str, gaps))
                if total > len(gaps):
                    preview += T(f" …（共 {total} 个，仅显示前 {len(gaps)} 个）",
                                 f" … ({total} total; showing first {len(gaps)})")
                gap_msgs.append(T(f"{label} 缺少编号: {preview}",
                                  f"{label} missing number(s): {preview}"))
        if gap_msgs:
            logger.add(LogLevel.WARN, CheckType.CONTENT,
                       T(f"图表序号可能存在跳号 ({'; '.join(gap_msgs)})，请确认编号是否连续。",
                         f"Figure/Table numbering may have gaps ({'; '.join(gap_msgs)}); please confirm the numbering is continuous."))
