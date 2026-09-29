from enum import Enum
from collections import defaultdict
from termcolor import colored

LANG = "zh"

def set_language(lang):
    global LANG
    LANG = lang if lang in ("zh", "en") else "zh"

def T(zh, en):
    """按当前语言返回中文或英文文案。"""
    return zh if LANG == "zh" else en

class LogLevel(Enum):
    ERROR = "Error"
    WARN = "Warning"
    PASS = "Pass"
    UNCHECKED = "Unchecked"

class CheckType(Enum):
    FORMAT = "Format"
    CONTENT = "Content"
    ANONYMITY = "Anonymity"
    LINK = "Link"

class CheckLogger:
    def __init__(self):
        self.logs = defaultdict(list)
        # 区分检查执行失败与成功检查后发现论文问题；支持跨进程汇总。
        self.execution_failed = False

    def add(self, level: LogLevel, check_type: CheckType, message: str):
        self.logs[level].append((check_type, message))

    def has_errors(self):
        return len(self.logs[LogLevel.ERROR]) > 0

    def has_issues(self):
        return len(self.logs[LogLevel.ERROR]) > 0 or len(self.logs[LogLevel.WARN]) > 0

    def has_unchecked(self):
        return bool(self.logs.get(LogLevel.UNCHECKED))

    def print_report(self, pdf_path):
        print(T(f"\n[{pdf_path}] 检查报告:", f"\n[{pdf_path}] Report:"))

        if LogLevel.PASS in self.logs:
            for c_type, msg in self.logs[LogLevel.PASS]:
                print(colored(f"  ✓ [{c_type.value}] {msg}", "green"))

        if LogLevel.UNCHECKED in self.logs:
            label = T("未检查", "Unchecked")
            for c_type, msg in self.logs[LogLevel.UNCHECKED]:
                print(colored(f"  ? {label} ({c_type.value}): {msg}", "white", "on_light_red"))

        if LogLevel.WARN in self.logs:
            for c_type, msg in self.logs[LogLevel.WARN]:
                print(colored(f"  ! Warning ({c_type.value}): {msg}", "yellow"))

        if LogLevel.ERROR in self.logs:
            for c_type, msg in self.logs[LogLevel.ERROR]:
                print(colored(f"  ✗ Error ({c_type.value}): {msg}", "red"))

        errors = len(self.logs[LogLevel.ERROR])
        warns = len(self.logs[LogLevel.WARN])

        if errors == 0 and warns == 0 and not self.has_unchecked():
            print(colored("All Clear!", "green", attrs=["bold"]))
        elif errors or warns:
            print(T(f"检测到 {errors} 个错误, {warns} 个警告。",
                    f"Detected {errors} error(s), {warns} warning(s)."))

        if self.has_unchecked():
            print(colored(T("检查未完成：存在未检查的内容，请人工复核。",
                            "Check incomplete: some content could not be checked; please review manually."),
                          "yellow"))
