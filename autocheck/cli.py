import os
import sys
import argparse
try:
    import pymupdf
except ImportError:  # 兼容 PyMuPDF < 1.24.3 的旧包名
    import fitz as pymupdf
from tqdm import tqdm
from multiprocessing.pool import Pool

from .core import CheckLogger, LogLevel, CheckType, set_language, T
from .format_check import FormatCheck
from .content_check import ContentCheck
from .anonymity_check import AnonymityCheck
from .link_check import LinkCheck

def process_pdf(args_tuple, progress_callback=None):
    pdf_path, options = args_tuple
    # 多进程场景下子进程需要同步语言设置
    set_language(options["lang"])
    logger = CheckLogger()

    def report_stage(stage):
        if progress_callback:
            progress_callback(stage)

    try:
        # 打开 PDF 文档供各个 Checker 共享，避免重复 IO
        with pymupdf.open(pdf_path) as doc:
            report_stage(T("排版格式检查", "Format check"))
            FormatCheck(
                max_pages=options["max_pages"],
                min_dpi=options["min_dpi"],
                margin_pt=options["margin_pt"],
            ).execute(pdf_path, doc, logger)

            report_stage(T("内容与引用排查", "Content check"))
            ContentCheck().execute(pdf_path, doc, logger)

            report_stage(T("匿名性检查", "Anonymity check"))
            AnonymityCheck().execute(pdf_path, doc, logger)

            check_links = not options["disable_link_check"]
            if check_links:
                report_stage(T("外部链接与参考文献检查", "Link & references check"))
            else:
                report_stage(T("参考文献检查（网络检查已关闭）", "References check (network disabled)"))
            LinkCheck().execute(pdf_path, doc, logger, check_links=check_links)

    except Exception as e:
        logger.execution_failed = True
        logger.add(LogLevel.ERROR, CheckType.FORMAT,
                   T(f"PDF 读取或检查失败: {str(e)}", f"PDF reading or checking failed: {str(e)}"))

    return pdf_path, logger


def main():
    parser = argparse.ArgumentParser(
        description="AutoPaperCheck - 论文排版与格式自动检查工具 / Automated paper formatting & submission checker")
    parser.add_argument('submission_paths', metavar='file_or_dir', nargs='+', default=[])
    parser.add_argument('--num_workers', type=int, default=1,
                        help="并行处理的工作进程数 / Number of parallel worker processes")
    parser.add_argument('--disable_link_check', action='store_true',
                        help="禁用 URL 网络死链检查，保留离线文献去重 / Disable URL network checking; keep offline reference deduplication")
    parser.add_argument('--max_pages', type=int, default=None,
                        help="最大页数限制（按目标会议单盲/双盲要求配置），默认不检查 / Max page limit (per venue rules); disabled by default")
    parser.add_argument('--min_dpi', type=int, default=150,
                        help="图片最低分辨率阈值 (DPI)，默认 150 / Minimum image DPI threshold, default 150")
    parser.add_argument('--margin_pt', type=float, default=None,
                        help="页边距溢出检查的固定版心内缩值 (pt)；默认从内容分布自动推断版心（支持双栏识别） / Fixed margin inset (pt) for overflow check; default auto-infers the text area from content")
    parser.add_argument('--lang', choices=['zh', 'en'], default='zh',
                        help="报告输出语言：zh 中文 / en English，默认 zh / Report language, default zh")

    args = parser.parse_args()
    set_language(args.lang)

    # 递归获取目录下的所有 PDF 文件
    fileset = set()
    input_failed = False

    def report_input_error(message):
        nonlocal input_failed
        input_failed = True
        print(message, file=sys.stderr)

    for path in args.submission_paths:
        if os.path.isfile(path) and path.lower().endswith(".pdf"):
            fileset.add(path)
        elif os.path.isdir(path):
            found_pdf = False
            for root, _, file_names in os.walk(
                    path, onerror=lambda error: report_input_error(
                        T(f"无法读取目录: {error}", f"Cannot read directory: {error}"))):
                for f in file_names:
                    if f.lower().endswith(".pdf"):
                        fileset.add(os.path.join(root, f))
                        found_pdf = True
            if not found_pdf:
                report_input_error(T(f"目录中未找到 PDF 文件: {path}",
                                     f"No PDF files found in directory: {path}"))
        else:
            report_input_error(T(f"输入不存在或不是 PDF 文件/目录: {path}",
                                 f"Input does not exist or is not a PDF file/directory: {path}"))

    fileset = sorted(list(fileset))

    if not fileset:
        print(T("未找到任何 PDF 文件。", "No PDF files found."))
        return 2

    options = {
        "disable_link_check": args.disable_link_check,
        "max_pages": args.max_pages,
        "min_dpi": args.min_dpi,
        "margin_pt": args.margin_pt,
        "lang": args.lang,
    }

    # 构建进程池参数
    task_args = [(f, options) for f in fileset]

    if args.num_workers > 1:
        # 多进程下 worker 隔离，无法实时回传阶段，按文件推进进度条
        with Pool(args.num_workers) as p:
            results = list(tqdm(p.imap(process_pdf, task_args), total=len(fileset),
                                desc=T("并行检查", "Checking (parallel)")))
    else:
        results = []
        with tqdm(total=len(fileset), desc=T("检查", "Checking")) as pbar:
            for t in task_args:
                file_name = os.path.basename(t[0])
                pbar.set_description(T(f"检查 {file_name}", f"Checking {file_name}"))

                def on_stage(stage):
                    pbar.set_postfix_str(stage)

                results.append(process_pdf(t, progress_callback=on_stage))
                pbar.set_postfix_str("")
                pbar.update(1)

    # 输出合并后的报告
    print("\n" + "="*50)
    files_with_issues = 0
    files_with_unchecked = 0
    for pdf_path, logger in results:
        logger.print_report(pdf_path)
        if logger.has_issues():
            files_with_issues += 1
        if logger.has_unchecked():
            files_with_unchecked += 1
    print("="*50)
    if files_with_unchecked:
        print(T(f"共检查 {len(results)} 个文件：{files_with_issues} 个存在错误或警告，"
                f"{files_with_unchecked} 个存在未检查内容。",
                f"Checked {len(results)} file(s); {files_with_issues} have errors or warnings; "
                f"{files_with_unchecked} file(s) have unchecked content."))
    else:
        print(T(f"共检查 {len(results)} 个文件，其中 {files_with_issues} 个存在问题。",
                f"Checked {len(results)} file(s); {files_with_issues} of them have issues."))
    print()
    if input_failed:
        return 2
    return 1 if any(logger.execution_failed for _, logger in results) else 0

if __name__ == "__main__":
    raise SystemExit(main())
