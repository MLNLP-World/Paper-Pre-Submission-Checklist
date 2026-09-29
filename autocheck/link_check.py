import re
import difflib
import unicodedata
from urllib.parse import urljoin
from urllib3.exceptions import HTTPError
from concurrent.futures import ThreadPoolExecutor
from .core import LogLevel, CheckType, T
from .anonymity_check import extract_urls
from .network import BlockedURL, request_public_url

from .reference_parser import (
    NUMBERED_ENTRY_PATTERN, AUTHOR_YEAR_PATTERN, _looks_like_authors,
    _year_end_author_prefix_end, _parse_reference_document,
)

SIMILARITY_THRESHOLD = 0.8
MIN_TITLE_LENGTH = 10
MAX_REDIRECTS = 5
MIN_FRAGMENT_CHARS = 80
FRAGMENT_SEED_WORDS = 10
# 常见会议/期刊模板语开头（归一化小写形式）；重复片段以此类文本开头时不报告。
FRAGMENT_BOILERPLATE_PREFIXES = (
    'in proceedings of', 'proceedings of the',
    'advances in neural information processing systems',
    'in computer vision', 'in european conference', 'arxiv preprint',
)


def _normalize_entry(entry):
    entry = NUMBERED_ENTRY_PATTERN.sub('', entry, count=1)
    entry = unicodedata.normalize('NFKC', entry).casefold()
    return ''.join(char for char in entry if char.isalnum() or char.isspace())


def _extract_reference_entries(doc):
    """Compatibility view: only complete segmentation returns an entry list.

    Internal callers use the structured result to retain entries even when
    another part of the bibliography could not be parsed.
    """
    parsed = _parse_reference_document(doc)
    if parsed.complete:
        return parsed.entries, None
    if not parsed.found:
        return None, None
    return None, ' '.join(item.text for item in parsed.unresolved)


def _normalize_title(title):
    title = unicodedata.normalize('NFKC', title).casefold()
    title = ''.join(char for char in title if char.isalnum() or char.isspace())
    return re.sub(r'\s+', ' ', title).strip()


def _extract_title(entry):
    """提取归一化题名用于精确比较；无法可靠定位题名时返回 None。"""
    text = NUMBERED_ENTRY_PATTERN.sub('', entry, count=1)
    match = AUTHOR_YEAR_PATTERN.match(text)
    if match:
        rest = text[match.end():]
    else:
        author_end = _year_end_author_prefix_end(text)
        if author_end is None:
            # ECCV 等样式用冒号分隔作者与题名（"Field, A., Stone, B.: Title"）。
            colon = re.match(r'(?P<authors>.+?):\s+', text)
            if colon is None or not _looks_like_authors(colon.group('authors')):
                return None
            author_end = colon.end()
        rest = text[author_end:]
    title = _normalize_title(rest.split('.', 1)[0])
    return title if len(title) >= MIN_TITLE_LENGTH else None


def _find_duplicate_fragments(text):
    """兜底检查：在合并文本中查找重复出现的长片段。

    以词级 n-gram 为种子向前扩展成极大重复片段，只报告长度达标、含年份、
    且不以会议/期刊模板语开头的片段；供无法分条时使用。
    """
    words = _normalize_title(text).split()
    if len(words) < 2 * FRAGMENT_SEED_WORDS:
        return []
    occurrences = {}
    for index in range(len(words) - FRAGMENT_SEED_WORDS + 1):
        occurrences.setdefault(' '.join(words[index:index + FRAGMENT_SEED_WORDS]), []).append(index)

    fragments = set()
    for positions in occurrences.values():
        if len(positions) < 2:
            continue
        for i, start in enumerate(positions):
            for other in positions[i + 1:]:
                length = FRAGMENT_SEED_WORDS
                while (other + length < len(words)
                       and words[start + length] == words[other + length]):
                    length += 1
                fragment = ' '.join(words[start:start + length])
                if len(fragment) >= MIN_FRAGMENT_CHARS:
                    fragments.add(fragment)

    # 只保留极大片段：被更长片段完整包含的碎片不再重复报告。
    maximal = [fragment for fragment in fragments
               if not any(fragment != other and fragment in other for other in fragments)]

    def plausible(fragment):
        if not re.search(r'\b(?:18|19|20)\d{2}\b', fragment):
            return False
        return not fragment.startswith(FRAGMENT_BOILERPLATE_PREFIXES)

    return sorted((fragment for fragment in maximal if plausible(fragment)),
                  key=len, reverse=True)


def _find_duplicate_references(doc, *, parsed=None):
    """返回 (题名完全相同的条目对, 其余文本高度相似的条目对)。

    返回 None 表示未定位到参考文献段；返回片段列表表示已定位但无法分条，
    此时结果为对合并文本做的受限重复片段检查。
    """
    if parsed is None:
        parsed = _parse_reference_document(doc)
    if not parsed.found:
        return None
    entries = parsed.entries
    if not entries:
        # Fragment matching is only supporting evidence; it never earns PASS.
        fallback_text = ' '.join(item.text for item in parsed.unresolved)
        return _find_duplicate_fragments(fallback_text)
    normalized = [_normalize_entry(entry) for entry in entries]
    titles = [_extract_title(entry) for entry in entries]

    def preview(entry):
        return entry[:60] + ("..." if len(entry) > 60 else "")

    identical = []
    identical_pairs = set()
    for i in range(len(entries)):
        if titles[i] is None:
            continue
        for j in range(i + 1, len(entries)):
            if titles[j] is not None and titles[i] == titles[j]:
                identical.append(f"\"{preview(entries[i])}\" = \"{preview(entries[j])}\"")
                identical_pairs.add((i, j))

    duplicates = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            # if (i, j) in identical_pairs:
            #     continue
            ratio = difflib.SequenceMatcher(
                None, normalized[i], normalized[j]
            ).ratio()
            if ratio > SIMILARITY_THRESHOLD:
                duplicates.append(f"\"{preview(entries[i])}\" ≈ \"{preview(entries[j])}\"")
    return identical, duplicates


class LinkCheck:
    def execute(self, pdf_path, doc, logger, *, check_links=True):
        # 网络开关只控制 URL 请求，离线参考文献检查始终执行。
        if check_links:
            self._check_urls(doc, logger)
        self._check_duplicate_references(doc, logger)

    def _check_urls(self, doc, logger):
        urls = extract_urls(doc)

        if not urls:
            return

        # 使用完整的浏览器请求头：许多站点（如 aclweb、arxiv）会拒绝
        # 极简 header 的请求（406/超时），但浏览器手动访问完全正常
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        broken_links = []    # 确认不可访问（404/410）
        unknown_links = []   # 无法自动验证（超时/403/406 等），需人工确认
        blocked_links = []   # 因访问边界被阻止，不能判断是否死链

        def verify(url):
            # 部分服务器不支持或拦截 HEAD，直接使用流式 GET
            for _ in range(2):  # 失败时重试一次
                try:
                    current = url
                    visited = set()
                    for hop in range(MAX_REDIRECTS + 1):
                        if current in visited:
                            return url, "unknown", T("重定向循环", "Redirect loop")
                        visited.add(current)
                        status, location = request_public_url(current, headers)
                        if status in (301, 302, 303, 307, 308):
                            if not location or hop == MAX_REDIRECTS:
                                return url, "unknown", T("重定向缺少目标或超过跳转上限", "Missing redirect target or redirect limit exceeded")
                            current = urljoin(current, location)
                            continue
                        if status in (404, 410):
                            return url, "dead", f"HTTP {status}"
                        if not 200 <= status < 300:
                            return url, "unknown", f"HTTP {status}"
                        return url, "ok", "OK"
                except BlockedURL as error:
                    return url, "blocked", str(error)
                except (HTTPError, OSError, ValueError):
                    continue
            return url, "unknown", T("连接、证书校验失败或超时", "Connection/TLS verification failed or timed out")

        with ThreadPoolExecutor(max_workers=5) as executor:
            for url, kind, status in executor.map(verify, urls):
                if kind == "dead":
                    broken_links.append(f"{url} ({status})")
                elif kind == "unknown":
                    unknown_links.append(f"{url} ({status})")
                elif kind == "blocked":
                    blocked_links.append(f"{url} ({status})")

        if blocked_links:
            logger.add(LogLevel.UNCHECKED, CheckType.LINK,
                       T("以下链接或其重定向目标不符合公网访问限制，已阻止请求，请人工核对: ",
                         "Requests blocked by public-network policy for these links or redirect targets; please review manually: ")
                       + ", ".join(sorted(blocked_links)))

        if broken_links:
            logger.add(LogLevel.ERROR, CheckType.LINK,
                       T(f"发现 {len(broken_links)} 个死链: ",
                         f"Found {len(broken_links)} broken link(s): ") + ", ".join(sorted(broken_links)))

        if unknown_links:
            logger.add(LogLevel.WARN, CheckType.LINK,
                       T(f"{len(unknown_links)} 个链接无法自动验证（可能被反爬拦截），请人工确认: ",
                         f"{len(unknown_links)} link(s) could not be verified automatically (possibly blocked by anti-bot); please check manually: ")
                       + ", ".join(sorted(unknown_links)))

        if not broken_links and not unknown_links and not blocked_links:
            logger.add(LogLevel.PASS, CheckType.LINK,
                       T(f"共 {len(urls)} 个外部链接，全部可以正常访问。",
                         f"All {len(urls)} external link(s) are reachable."))

    def _check_duplicate_references(self, doc, logger):
        parsed = _parse_reference_document(doc)
        result = _find_duplicate_references(doc, parsed=parsed)

        if result is None:
            logger.add(LogLevel.UNCHECKED, CheckType.LINK,
                       T("未定位到参考文献段，或无法可靠识别参考文献条目；重复条目检查未完成，请人工复核。",
                         "References section not found or entries could not be reliably separated; "
                         "duplicate-entry check incomplete, please review manually."))
            return
        if not parsed.complete:
            pages = sorted({item.page for item in parsed.unresolved})
            locations = ', '.join(str(page) for page in pages[:10])
            if len(pages) > 10:
                locations += ', ...'
            previews = '; '.join(item.text[:80] for item in parsed.unresolved[:3] if item.text)
            logger.add(LogLevel.UNCHECKED, CheckType.LINK,
                       T(f"已识别 {len(parsed.entries)} 条参考文献，但第 {locations} 页仍有无法可靠分条的内容；"
                         "重复条目检查未覆盖全部参考文献，请人工复核。",
                         f"Identified {len(parsed.entries)} reference entries, but unresolved content remains "
                         f"on page(s) {locations}; duplicate checking does not cover all references. Please review manually.")
                       + (' ' + previews if previews else ''))
        if isinstance(result, list):
            if result:
                previews = [fragment[:80] + ("..." if len(fragment) > 80 else "")
                            for fragment in result[:5]]
                logger.add(LogLevel.WARN, CheckType.LINK,
                           T(f"基于文本片段匹配发现 {len(result)} 处可能重复的长片段（可能漏报或误报）: ",
                             f"Found {len(result)} potentially duplicated long text fragment(s) "
                             f"via fragment matching (may miss or misreport duplicates): ")
                           + "; ".join(previews))
            return
        identical, duplicates = result
        if identical:
            logger.add(LogLevel.ERROR, CheckType.LINK,
                       T(f"参考文献中存在 {len(identical)} 组题名完全相同的条目，基本可确定为同一篇文献: ",
                         f"Found {len(identical)} pair(s) of reference entries with identical titles, "
                         f"almost certainly the same paper: ")
                       + "; ".join(identical[:5]))
        if duplicates:
            logger.add(LogLevel.WARN, CheckType.LINK,
                       T(f"参考文献中可能存在 {len(duplicates)} 组重复条目: ",
                         f"Found {len(duplicates)} potential duplicate reference entr(y/ies): ")
                       + "; ".join(duplicates[:5]))
        if parsed.complete and not identical and not duplicates:
            logger.add(LogLevel.PASS, CheckType.LINK,
                       T(f"已检查 {len(parsed.entries)} 条参考文献，未发现明显重复条目。",
                         f"Checked {len(parsed.entries)} reference entries. No obvious duplicate entries found in references."))
