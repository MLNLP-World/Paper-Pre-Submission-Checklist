import re
from .core import LogLevel, CheckType, T

# 本地编译路径模式（可能泄露电脑用户名）
LOCAL_PATH_PATTERN = re.compile(r'([A-Za-z]:\\Users\\[^\\\s]+|/Users/[^/\s]+|/home/[^/\s]+)')

# 首页机构/身份关键词
AFFILIATION_PATTERN = re.compile(
    r'\b(University|College|Institute|Institute of|Laboratory|Academy|Corporation|Company|School of|Department of)\b',
    re.IGNORECASE,
)

# 支持学校子域及多级后缀，不限定为 edu/com 等少数顶级域名。
EMAIL_PATTERN = re.compile(
    r"[\w.!#$%&'*+/=?^`{|}~-]+@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}\b",
    re.IGNORECASE,
)

# 只豁免完整的匿名占位值，避免放过同时包含占位词和真实姓名的字段。
ANONYMOUS_AUTHOR_VALUES = {
    "anonymous", "anonymous author", "anonymous authors", "anonymous submission", "匿名作者",
}

# 先提取包含圆括号的 URL 候选，再区分地址内部括号和正文外围括号。
TEXT_URL_PATTERN = re.compile(r'(https?://[^\s\]>}"\'<>]+|doi\.org/[^\s\]>}"\'<>]+)')

# 需要在匿名投稿中警惕的链接域名
NON_ANONYMOUS_DOMAINS = (
    "github.com",
    "drive.google.com",
    "dropbox.com",
    "huggingface.co",
    "github.io",
    "gitlab.com",
    "bitbucket.org",
)


def _trim_text_url(url):
    """保留地址内配对的圆括号，在外围右括号处结束并去除句末标点。"""
    depth = 0
    for index, char in enumerate(url):
        if char == '(':
            depth += 1
        elif char == ')':
            if depth == 0:
                url = url[:index]
                break
            depth -= 1
    return url.rstrip('.,;:')


def _normalize_text_url(url):
    url = _trim_text_url(url)
    return 'https://' + url if url.startswith('doi.org/') else url


def _word_in_link(word, link):
    """使用文字中心点，容忍链接矩形与字体边界间的少量舍入误差。"""
    rect = link.get('from')
    if rect is None:
        return False
    x, y = (word[0] + word[2]) / 2, (word[1] + word[3]) / 2
    return rect[0] - 1 <= x <= rect[2] + 1 and rect[1] - 1 <= y <= rect[3] + 1


def _is_wrapped_link_prefix(words, index, url, links):
    """仅在当前位置的链接文字能拼出 Annotation 目标时排除断行前缀。

    不能按全局前缀去重：同页另一处的站点首页可能是独立有效的链接。
    也不能仅凭换行拼接正文；Annotation 提供完整目标和续行位置证据。
    """
    first_word = words[index]
    targets = {link['uri'] for link in links
               if link['uri'] != url and link['uri'].startswith(url)
               and _word_in_link(first_word, link)}
    for target in targets:
        target_links = [link for link in links if link['uri'] == target]
        prefix = url
        previous_word = first_word
        # 正常 URL 只占少量相邻词；限制扫描量，避免把远处的同目标链接拼入。
        for word in words[index + 1:index + 65]:
            height = max(previous_word[3] - previous_word[1], word[3] - word[1], 1)
            if word[1] > previous_word[3] + 2 * height or word[3] < previous_word[1] - height:
                break
            if not any(_word_in_link(word, link) for link in target_links):
                continue  # 审稿行号等不属于链接的文字。
            combined = prefix + word[4]
            if _trim_text_url(combined) == target:
                return True
            if not target.startswith(combined):
                break
            prefix, previous_word = combined, word
    return False


def _extract_page_text_urls(page, links):
    # 坐标用于区分同一链接的断行片段与其他位置的独立短链接。
    # 兼容仅提供基本 get_text()/get_links() 接口的文本页面适配器。
    try:
        words = page.get_text('words')
    except (TypeError, AttributeError):
        words = None
    if not isinstance(words, (list, tuple)):
        for match in TEXT_URL_PATTERN.findall(page.get_text()):
            yield _normalize_text_url(match)
        return
    for index, word in enumerate(words):
        for match in TEXT_URL_PATTERN.findall(word[4]):
            url = _normalize_text_url(match)
            if not _is_wrapped_link_prefix(words, index, url, links):
                yield url


def extract_urls(doc):
    """提取文档 Annotation 与正文文本中的所有外部 URL/DOI，去重返回。"""
    urls = set()
    for page in doc:
        links = []
        for link in page.get_links():
            uri = link.get('uri', '')
            if uri.startswith('http') or uri.startswith('doi'):
                if uri.startswith('doi.org/'):
                    uri = 'https://' + uri
                urls.add(uri)
                links.append(dict(link, uri=uri))
        urls.update(_extract_page_text_urls(page, links))
    return urls


class AnonymityCheck:
    def execute(self, pdf_path, doc, logger):
        # 1. 检查元数据
        meta = doc.metadata
        author = meta.get("author", "") or ""
        creator = meta.get("creator", "")
        producer = meta.get("producer", "")

        normalized_author = " ".join(author.split()).casefold()
        if normalized_author and normalized_author not in ANONYMOUS_AUTHOR_VALUES:
            logger.add(LogLevel.ERROR, CheckType.ANONYMITY,
                       T(f"PDF元数据可能包含作者身份信息: Author='{author}'，请核对匿名要求。",
                         f"PDF metadata may contain author identity: Author='{author}'; please review anonymity requirements."))

        if creator and "word" in creator.lower():
            logger.add(LogLevel.WARN, CheckType.ANONYMITY,
                       T(f"PDF创建工具为 '{creator}', 建议使用 LaTeX 编译最终版。",
                         f"PDF creator is '{creator}'; LaTeX is recommended for the final version."))

        # 元数据中残留本地编译路径（含电脑用户名）
        for field_name, field_value in (("Creator", creator), ("Producer", producer)):
            if field_value:
                path_match = LOCAL_PATH_PATTERN.search(field_value)
                if path_match:
                    logger.add(LogLevel.WARN, CheckType.ANONYMITY,
                               T(f"PDF元数据 {field_name} 中包含本地路径 '{path_match.group(1)}'，可能泄露电脑用户名。",
                                 f"PDF metadata {field_name} contains a local path '{path_match.group(1)}', which may leak your computer username."))

        # 2. 检查首页机构/身份信息关键词
        if doc.page_count > 0:
            first_page_text = doc[0].get_text()
            hits = sorted({m.group(0) for pattern in (AFFILIATION_PATTERN, EMAIL_PATTERN)
                           for m in pattern.finditer(first_page_text)})
            if hits:
                preview = ", ".join(hits[:5]) + (" ..." if len(hits) > 5 else "")
                logger.add(LogLevel.WARN, CheckType.ANONYMITY,
                           T(f"首页检测到可能的机构/身份信息关键词 ({preview})，匿名投稿请确认作者信息已删除/隐藏。",
                             f"Possible affiliation/identity keyword(s) on first page ({preview}); for anonymous submission, confirm author info is removed/hidden."))

        # 3. 检查正文致谢
        ack_pages = []
        for page_num in range(doc.page_count):
            text = doc[page_num].get_text()
            if re.search(r'\b(Acknowledgments?|Acknowledgements?)\b', text, re.IGNORECASE):
                ack_pages.append(str(page_num + 1))

        if ack_pages:
            pages = ', '.join(ack_pages)
            logger.add(LogLevel.WARN, CheckType.ANONYMITY,
                       T(f"在第 {pages} 页发现致谢(Acknowledgments)，请确保其符合当前投稿阶段的匿名要求。",
                         f"Acknowledgments found on page(s) {pages}; ensure it complies with the anonymity requirements of the current submission stage."))

        # 4. 链接合规性（不发起网络请求）
        flagged_urls = []
        for url in extract_urls(doc):
            lowered = url.lower()
            if any(domain in lowered for domain in NON_ANONYMOUS_DOMAINS) or "/~" in lowered:
                flagged_urls.append(url)

        if flagged_urls:
            preview = ", ".join(sorted(flagged_urls)[:5]) + (" ..." if len(flagged_urls) > 5 else "")
            logger.add(LogLevel.WARN, CheckType.ANONYMITY,
                       T(f"检测到 {len(flagged_urls)} 个可能泄露身份的链接 ({preview})，匿名投稿建议替换为匿名链接（如 Anonymous GitHub）。",
                         f"Detected {len(flagged_urls)} link(s) that may reveal identity ({preview}); for anonymous submission, consider replacing them with anonymous links (e.g. Anonymous GitHub)."))
