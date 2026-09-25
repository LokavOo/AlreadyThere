"""题目包（1.8 起可按项目选用）：问题文件里写 "task": "nobel" 等；不写的按事实汇编（出生日期）处理。"""

from importlib import import_module

TASKS = {"fact_compilation": "fact_compilation", "nobel": "nobel"}


def load(cfg):
    name = (cfg or {}).get("task") or "fact_compilation"
    if name not in TASKS:
        raise ValueError(f"未知的题目包：{name}（可选：{'、'.join(TASKS)}）")
    return import_module(f".{TASKS[name]}", __name__)
