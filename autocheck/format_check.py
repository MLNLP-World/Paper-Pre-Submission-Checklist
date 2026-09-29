import os
import re
from .core import LogLevel, CheckType, T
from .pdf_text import get_review_number_bboxes

# 判断是否已嵌入的字体文件 key（位于字体的 FontDescriptor 对象中）
FONT_FILE_KEYS = ("FontFile", "FontFile2", "FontFile3")

# PDF 标准 14 字体：规范允许不嵌入，阅读器必须内置替代字体
BASE14_FONTS = {
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Symbol", "ZapfDingbats",
}

# 子集字体前缀（如 BCDEEE+TimesNewRomanPSMT）
SUBSET_PREFIX_PATTERN = re.compile(r'^[A-Z]{6}\+')


def _is_font_embedded(doc, xref, depth=0):
    """沿引用链检查字体是否嵌入：字体对象 -> FontDescriptor / DescendantFonts。

    /FontFile 位于 FontDescriptor 子对象中；Type0 复合字体需先下钻到
    DescendantFonts 中的 CIDFont 再找其 FontDescriptor。
    """
    if depth > 3 or not 0 < xref < doc.xref_length():
        return False  # 异常引用链不能作为“已嵌入”的证据。
    try:
        for key in FONT_FILE_KEYS:
            kind, value = doc.xref_get_key(xref, key)
            if kind == "xref":
                stream_xref = int(value.split()[0])
                if (0 < stream_xref < doc.xref_length()
                        and doc.xref_is_stream(stream_xref)
                        and doc.xref_stream(stream_xref)):
                    return True

        kind, descriptor = doc.xref_get_key(xref, "FontDescriptor")
        if kind == "xref" and _is_font_embedded(doc, int(descriptor.split()[0]), depth + 1):
            return True
        if kind == "dict":
            # PDF also permits a descriptor dictionary directly inside a font.
            for key in FONT_FILE_KEYS:
                value_kind, value = doc.xref_get_key(xref, "FontDescriptor/" + key)
                if value_kind == "xref":
                    stream_xref = int(value.split()[0])
                    if (0 < stream_xref < doc.xref_length()
                            and doc.xref_is_stream(stream_xref)
                            and doc.xref_stream(stream_xref)):
                        return True

        kind, descendants = doc.xref_get_key(xref, "DescendantFonts")
        if kind == "xref":
            descendants = doc.xref_object(int(descendants.split()[0]))
        child_refs = re.findall(r'(\d+)\s+\d+\s+R', descendants)
        return bool(child_refs) and all(_is_font_embedded(doc, int(child), depth + 1)
                                        for child in child_refs)
    except (ValueError, RuntimeError):
        return False

# 自适应版心推断参数
EDGE_ALIGNMENT_PT = 3.0     # 同一对齐位置的坐标容差，避免缩进形成连锁聚类
MIN_CLUSTER_RATIO = 0.03    # 过滤样本过少的边缘聚类（缩进/居中噪声）
OVERFLOW_TOLERANCE_PT = 3.0 # 允许的超出版心容差
MIN_LINES_FOR_INFERENCE = 10
MIN_BODY_LINE_WIDTH_PT = 80.0
MIN_BODY_LINE_CHARS = 20


def _review_header_bboxes(page, blocks):
    """Recognize the matched submission label and review notice in CV headers.

    Only matching conference/ID labels next to a standard review notice are
    exempt. Arbitrary text at the top of a page remains subject to the check.
    """
    top_lines = []
    rect = page.rect
    for block in blocks:
        for line in block.get("lines", []):
            bbox = tuple(line["bbox"])
            if (rect.x0 <= bbox[0] <= bbox[2] <= rect.x1
                    and rect.y0 <= bbox[1] <= bbox[3] <= rect.y0 + 60):
                text = " ".join("".join(span["text"] for span in line["spans"]).split())
                top_lines.append((text, bbox))
    ignored = set()
    for text, bbox in top_lines:
        match = re.fullmatch(
            r'([A-Z][A-Z0-9-]{1,15})\s+\d{4}\s+Submission\s+#(\d+)\.\s*'
            r'CONFIDENTIAL REVIEW COPY\.\s*DO NOT DISTRIBUTE\.', text)
        if match:
            labels = {match.group(1), "#" + match.group(2)}
            ignored.add(bbox)
            ignored.update(box for label, box in top_lines if label in labels)
    return ignored


def _collect_text_layout(page):
    """用正文行推断栏位，用去除页边数字后的文本块检查溢出。"""
    blocks = []
    # flags=0 不提取图片数据；位图边界由 get_image_info 单独提供。
    raw_blocks = page.get_text("dict", flags=0)["blocks"]
    review_numbers = get_review_number_bboxes(page, raw_blocks)
    review_headers = _review_header_bboxes(page, raw_blocks)
    for block in raw_blocks:
        lines = []
        for line in block.get("lines", []):
            text = ''.join(span["text"] for span in line["spans"]).strip()
            if text:
                lines.append((text, line["bbox"]))
        blocks.append(lines)

    rect = page.rect

    def is_margin_number(text, bbox):
        if tuple(bbox) in review_numbers or tuple(bbox) in review_headers:
            return True
        if text != str(page.number + 1) or bbox[0] < rect.x0 or bbox[2] > rect.x1:
            return False
        if bbox[1] < rect.y0 + 30 or bbox[3] > rect.y1 - 50:
            return True  # 页眉、页脚区域的独立数字（通常为页码）。
        return False

    text_bboxes = []
    text_edges = []
    for lines in blocks:
        kept = [(text, bbox) for text, bbox in lines if not is_margin_number(text, bbox)]
        if not kept:
            continue
        # 保留块级结构，避免将正常跨栏标题、表格拆成侵入栏间空隙的片段。
        text_bboxes.append((min(b[0] for _, b in kept), min(b[1] for _, b in kept),
                            max(b[2] for _, b in kept), max(b[3] for _, b in kept)))
        # 短标题、数字、表格单元格不用于推断，但仍参与上面的边界检查。
        text_edges.extend((bbox[0], bbox[2]) for text, bbox in kept
                          if not text.isdigit() and len(text) >= MIN_BODY_LINE_CHARS
                          and bbox[2] - bbox[0] >= MIN_BODY_LINE_WIDTH_PT)
    return text_bboxes, text_edges


def _percentile(sorted_vals, p):
    idx = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def _detect_bands(edges):
    """根据正文行的主要对齐位置推断内容栏带 [(left, right), ...]。

    左边缘只在小容差内聚类，优先保留支持行数最多的栏；左界取该组中位数，
    右界取 95 分位数。少量越界行和位于正文栏内的缩进组不会扩大版心。
    """
    clusters = []
    for edge in sorted(edges):
        if clusters and edge[0] - clusters[-1][0][0] <= EDGE_ALIGNMENT_PT:
            clusters[-1].append(edge)
        else:
            clusters.append([edge])

    min_size = max(3, MIN_CLUSTER_RATIO * len(edges))
    candidates = []
    for cluster in clusters:
        if len(cluster) < min_size:
            continue
        left = _percentile(sorted(x0 for x0, _ in cluster), 0.5)
        right = _percentile(sorted(x1 for _, x1 in cluster), 0.95)
        if right > left:
            candidates.append((len(cluster), left, right))

    bands = []
    for _, left, right in sorted(candidates, key=lambda item: (-item[0], item[1])):
        if not any(left < r + EDGE_ALIGNMENT_PT and right > l - EDGE_ALIGNMENT_PT
                   for l, r in bands):
            bands.append((left, right))
    return sorted(bands)


def _is_overflow_bbox(bbox, bands, page_rect, tolerance=OVERFLOW_TOLERANCE_PT):
    x0, _, x1, _ = bbox
    # 超出页面物理边界
    if x0 < page_rect.x0 or x1 > page_rect.x1:
        return True
    if not bands:
        return False

    outer_left = min(b[0] for b in bands)
    outer_right = max(b[1] for b in bands)

    if x0 < outer_left - tolerance or x1 > outer_right + tolerance:
        return True
    if any(x0 >= left - tolerance and x1 <= right + tolerance for left, right in bands):
        return False

    # 居中且覆盖多个栏的元素无需铺满版心；单栏正文仅伸入栏间空隙仍应报警。
    center_offset = abs((x0 + x1 - outer_left - outer_right) / 2)
    center_tolerance = max(tolerance, (outer_right - outer_left) * 0.05)
    covered_bands = sum(x0 < right - tolerance and x1 > left + tolerance
                        for left, right in bands)
    if covered_bands >= 2 and center_offset <= center_tolerance:
        return False
    return True


class FormatCheck:
    def __init__(self, size_limit_mb=10.0, max_pages=None, min_dpi=150, margin_pt=None):
        self.size_limit = size_limit_mb
        self.max_pages = max_pages
        self.min_dpi = min_dpi
        # None 表示自动从内容分布推断版心；指定数值则使用固定边距
        self.margin_pt = margin_pt

    def execute(self, pdf_path, doc, logger):
        # 1. 检查文件大小
        file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
        if file_size_mb > self.size_limit:
            logger.add(LogLevel.WARN, CheckType.FORMAT,
                       T(f"文件大小 {file_size_mb:.2f}MB 超过常规 {self.size_limit}MB 限制。",
                         f"File size {file_size_mb:.2f}MB exceeds the typical {self.size_limit}MB limit."))
        else:
            logger.add(LogLevel.PASS, CheckType.FORMAT,
                       T(f"文件大小合规 ({file_size_mb:.2f}MB)。",
                         f"File size OK ({file_size_mb:.2f}MB)."))

        # 2. 检查页数限制（按目标会议单盲/双盲要求通过 --max_pages 配置）
        if self.max_pages is not None:
            if doc.page_count > self.max_pages:
                logger.add(LogLevel.ERROR, CheckType.FORMAT,
                           T(f"总页数 {doc.page_count} 超过设定的 {self.max_pages} 页限制。",
                             f"Total page count {doc.page_count} exceeds the configured {self.max_pages}-page limit."))
            else:
                logger.add(LogLevel.PASS, CheckType.FORMAT,
                           T(f"总页数 {doc.page_count} 未超过 {self.max_pages} 页限制。",
                             f"Total page count {doc.page_count} is within the {self.max_pages}-page limit."))

        # 3. 逐页收集：空白页 / 字体嵌入 / 图片分辨率 / 文本与图片边界框
        blank_pages = []
        unembedded_fonts = set()
        base14_fonts = set()
        checked_font_xrefs = {}  # xref -> 是否嵌入，同一字体跨页只解析一次
        low_dpi_images = []
        text_edges = []      # 全文正文行的 (x0, x1)，用于版心推断
        page_items = []      # 每页的 (文本块 bbox 列表, 图片 bbox 列表)

        for page_num in range(doc.page_count):
            page = doc[page_num]
            text = page.get_text().strip()

            image_infos = page.get_image_info()

            # 没有文字或位图时，还需检查矩形、线条、曲线等矢量绘图。
            if not text and not image_infos and not page.get_drawings():
                blank_pages.append(page_num + 1)

            # 字体嵌入检查：沿 FontDescriptor/DescendantFonts 引用链查找嵌入的字体文件；
            # 未嵌入的 Base-14 标准字体由阅读器保证可显示，降级为警告
            for font in page.get_fonts():
                xref, font_name = font[0], font[3]
                if xref not in checked_font_xrefs:
                    checked_font_xrefs[xref] = _is_font_embedded(doc, xref)
                if not checked_font_xrefs[xref]:
                    base_name = SUBSET_PREFIX_PATTERN.sub('', font_name)
                    if base_name in BASE14_FONTS:
                        base14_fonts.add(base_name)
                    else:
                        unembedded_fonts.add(base_name)

            # 图片分辨率检查：有效 DPI = 像素数 / (渲染尺寸 pt / 72)
            for info in image_infos:
                bbox = info.get("bbox")
                if not bbox:
                    continue
                width_pt = bbox[2] - bbox[0]
                height_pt = bbox[3] - bbox[1]
                if width_pt <= 0 or height_pt <= 0:
                    continue
                dpi_x = info.get("width", 0) / (width_pt / 72)
                dpi_y = info.get("height", 0) / (height_pt / 72)
                dpi = min(dpi_x, dpi_y)
                if 0 < dpi < self.min_dpi:
                    low_dpi_images.append(T(f"第 {page_num + 1} 页 ({dpi:.0f} DPI)",
                                            f"page {page_num + 1} ({dpi:.0f} DPI)"))

            # 收集边界框供溢出检测
            text_bboxes, page_edges = _collect_text_layout(page)
            image_bboxes = [info["bbox"] for info in image_infos if info.get("bbox")]
            page_items.append((text_bboxes, image_bboxes))
            text_edges.extend(page_edges)

        if blank_pages:
            logger.add(LogLevel.ERROR, CheckType.FORMAT,
                       T(f"发现空白页: 第 {', '.join(map(str, blank_pages))} 页。",
                         f"Blank page(s) found: page {', '.join(map(str, blank_pages))}."))

        if unembedded_fonts:
            logger.add(LogLevel.ERROR, CheckType.FORMAT,
                       T(f"发现未嵌入的字体: {', '.join(sorted(unembedded_fonts))}",
                         f"Unembedded fonts found: {', '.join(sorted(unembedded_fonts))}"))

        if base14_fonts:
            logger.add(LogLevel.WARN, CheckType.FORMAT,
                       T(f"以下标准字体未嵌入 ({', '.join(sorted(base14_fonts))})，阅读器通常可替代显示；"
                         f"若投稿系统要求全部字体嵌入（如 IEEE PDF eXpress），请在编译时开启字体嵌入。",
                         f"Standard font(s) not embedded ({', '.join(sorted(base14_fonts))}); viewers usually substitute them. "
                         f"If the submission system requires all fonts embedded (e.g. IEEE PDF eXpress), enable font embedding when compiling."))

        if not unembedded_fonts and not base14_fonts:
            logger.add(LogLevel.PASS, CheckType.FORMAT,
                       T("所有使用到的字体均已嵌入。", "All fonts used are embedded."))

        if low_dpi_images:
            suffix = " ..." if len(low_dpi_images) > 10 else ""
            logger.add(LogLevel.WARN, CheckType.FORMAT,
                       T(f"发现 {len(low_dpi_images)} 张图片分辨率低于 {self.min_dpi} DPI，可能模糊: ",
                         f"{len(low_dpi_images)} image(s) below {self.min_dpi} DPI may appear blurry: ")
                       + ", ".join(low_dpi_images[:10]) + suffix)

        # 4. 页边距溢出检查
        self._check_margin_overflow(doc, page_items, text_edges, logger)

    def _check_margin_overflow(self, doc, page_items, text_edges, logger):
        """检测内容是否横向超出版心。

        默认从全文正文行边缘分布自动推断版心（支持单/双栏识别），
        避免对固定边距值的硬编码依赖；指定 --margin_pt 时退回固定边距模式。
        """
        inferred_bands = []
        inference_note = ""

        if self.margin_pt is None and len(text_edges) >= MIN_LINES_FOR_INFERENCE:
            inferred_bands = _detect_bands(text_edges)
            if inferred_bands:
                band_desc = ", ".join(f"[{l:.0f}, {r:.0f}]" for l, r in inferred_bands)
                inference_note = T(f"（自动推断版心 {band_desc} pt，共 {len(inferred_bands)} 栏）",
                                   f" (auto-inferred text area {band_desc} pt, {len(inferred_bands)} column(s))")

        overflow_pages = set()
        for page_num, (text_bboxes, image_bboxes) in enumerate(page_items):
            page_rect = doc[page_num].rect
            bands = inferred_bands
            if not bands:
                # 固定边距按每页尺寸计算；推断样本不足时沿用默认 36pt。
                margin = self.margin_pt if self.margin_pt is not None else 36.0
                bands = [(page_rect.x0 + margin, page_rect.x1 - margin)]
            for bbox in text_bboxes + image_bboxes:
                if _is_overflow_bbox(bbox, bands, page_rect):
                    overflow_pages.add(page_num + 1)
                    break

        if overflow_pages:
            pages = ', '.join(map(str, sorted(overflow_pages)))
            logger.add(LogLevel.WARN, CheckType.FORMAT,
                       T(f"第 {pages} 页存在内容横向超出版心或页面边界{inference_note}，请人工复核是否溢出页边距。",
                         f"Content on page(s) {pages} extends horizontally beyond the text area or page edge{inference_note}; please verify margin overflow manually."))
