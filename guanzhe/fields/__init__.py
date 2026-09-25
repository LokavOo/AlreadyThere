"""字段类型与程序核对器（1.7）。

题目包声明每个字段是什么类型，主流程按类型调用对应的核对器。
目前有：日期（含农历插件）。以后加：人名名单、引文等。
"""

from .date import DateField, format_date, normalize_date

__all__ = ["DateField", "format_date", "normalize_date"]
