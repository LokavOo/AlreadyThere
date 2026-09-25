"""读取设置与密钥。

- settings.json：模型接入、角色分配、流程参数（可以公开，不含密钥）。
- secrets.env：密钥，每行 名称=值。只由用户自己填写，程序只读取，不写入账本。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PARAMS = {
    # 0.3 调用失败重试（草案初始值）
    "retry_batch1": [10, 10, 10],
    "retry_batch2": [60, 120, 240],
    "call_timeout_seconds": 300,          # 原型前须实测；先给一个宽松值
    # 流程
    "max_rounds": 6,                      # 1.4 起为 6
    "search_results_per_query": 3,
    "items_per_call": 6,                 # 每次调用处理的条目数，控制输入长度
    "max_sources_shown": 4,              # 每个条目最多展示的检索来源数（按"是否讲到出生"挑，不再只取最新）
    "independent_sources": 2,            # 审查者独立核验时最多附几个独立来源
}

# 1.4 记忆模式（每个角色可在设置里写 "memory"）：
#   全局：能看到所有角色上一轮的结果与意见原文
#   固定：只看 see 里列出的角色（"self" 表示自己）、最近 rounds 轮
#   全新：每轮都从头开始，只看问题、口径、本轮检索和针对自己的意见（意见中不含他方的数值与出处）
DEFAULT_MEMORY = {
    "生产者": {"mode": "固定", "see": ["self"], "rounds": 1},
    "审查者": {"mode": "全局"},
}


def memory_of(role_settings: dict) -> dict:
    m = dict(DEFAULT_MEMORY.get(role_settings.get("kind"), {"mode": "全新"}))
    m.update(role_settings.get("memory") or {})
    if m.get("mode") not in ("全局", "固定", "全新"):
        raise ValueError(f"memory.mode 只能是 全局/固定/全新，现在是 {m.get('mode')!r}")
    m.setdefault("see", ["self"])
    m.setdefault("rounds", 1)
    return m


def load_secrets(path: str | os.PathLike | None) -> dict:
    secrets = {}
    if path and Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            secrets[k.strip()] = v.strip().strip('"').strip("'")
    # 环境变量优先
    for k in list(secrets) + ["ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
                              "ARK_API_KEY", "TAVILY_API_KEY"]:
        if os.environ.get(k):
            secrets[k] = os.environ[k]
    return secrets


def load_settings(path: str | os.PathLike) -> dict:
    s = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    params = dict(DEFAULT_PARAMS)
    params.update(s.get("params", {}))
    s["params"] = params
    return s
