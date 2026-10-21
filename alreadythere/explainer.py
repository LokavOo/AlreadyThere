"""2.10：解说者（用户 2026-10-02～10-21 定）。

- 主窗口旁的小边栏，有自己的输入框。用户问"现在在做什么、做过什么、准备做什么、Kimi 在想什么"时，用通俗的话回答。
- 只读不写：能读到题目、账本里的过程、各角色状态和正在进行的思考；它的话不进流程、不发给任何角色、不是验证结果。
- 没有"用这个问法建题"的按钮：题目始终由用户自己在主窗口输入框里提交（用户定）。解说者可以指出题目哪里没说清楚，
  提出"你想问的可能是……"时写明是猜测（规格 3.8）。
- 看多少内容：按大模型聊天的通用做法——能放进上下文的尽量放，放不下时先舍弃最早的记录，最新的始终保留。
  上下文大小取设置里这个角色的 context_window（token），按每个字约 1 个 token 保守估算，并给回答留出余量。
- 便宜的模型就够用（用户：网页版都够用了）；接入时在设置示例、窗口和使用说明里提示。

为什么不写进项目账本：流程运行时账本由运行进程写（带哈希链，同一时刻只能有一个写入者），解说者的对话由窗口进程写，
两边同时写会把链写乱。所以对话单独存在项目文件夹的「解说者/对话.jsonl」，同样逐条带哈希链；没选项目时存在数据文件夹的
「解说者_未建题对话.jsonl」。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import feed

SYSTEM = """你是观者自在程序里的【解说者】。观者自在让几个大模型分工完成同一道题：生产者各自作答，审查者检查答卷，找茬者找共同的前提和冲突，程序负责核对引文、比对答案、运行代码、记账。
你的任务：用通俗的话回答用户关于这个项目的问题——流程现在在做什么、已经做了什么、接下来做什么、各角色在讨论什么、某个角色为什么还没有返回。
规则：
1. 只依据下面提供的材料（题目、项目记录、角色状态、正在进行的思考）回答。材料里没有的，写"记录里没有"。
2. 提到记录里的具体事情时，写出它的序号（例如 #123）。
3. 你的话不进入流程，不发给任何角色，不是验证结果。不判断哪个答案正确；用户问谁对时，说明审查者和程序核对的结论分别是什么。
4. 不替用户写题目。可以指出题目里没说清楚的地方；提出"你想问的可能是……"时，写明这是猜测。题目由用户自己在主窗口输入框里提交。
5. 概括思考内容，不大段照抄原文。
6. 用户没有数学和编程背景：少用术语，用到时用一句话解释。
7. 材料里写着"更早的记录已省略"时，说明你看到的不是全部；涉及更早的内容时如实说明。"""

DEFAULT_WINDOW = 64000   # 设置里没写 context_window 时按这个算（token）
ANSWER_RESERVE = 6000    # 给回答留的余量（token）


def find_role(settings: dict):
    for r, v in (settings.get("roles") or {}).items():
        if v.get("kind") == "解说者":
            return r, v
    return None, None


def log_path(base: Path, name: str | None) -> Path:
    if name:
        return Path(base) / name / "解说者" / "对话.jsonl"
    return Path(base) / "解说者_未建题对话.jsonl"


def history(base: Path, name: str | None) -> list:
    f = log_path(base, name)
    out = []
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _append(base, name, rec):
    f = log_path(base, name)
    f.parent.mkdir(parents=True, exist_ok=True)
    hist = history(base, name)
    prev = hist[-1]["hash"] if hist else ""
    body = dict(rec, prev=prev)
    body["hash"] = hashlib.sha256((prev + json.dumps(rec, ensure_ascii=False, sort_keys=True)).encode("utf-8")).hexdigest()
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(body, ensure_ascii=False) + "\n")
    return body


def _material(base: Path, name: str | None, running: bool):
    """返回 (固定部分, 记录行列表[从旧到新], 思考部分)。"""
    if not name:
        return "（还没有选项目：用户可能在想怎么问。）", [], ""
    root = Path(base) / name
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {}
    head = [f"【项目】{name}"]
    for it in cfg.get("items", [])[:20]:
        head.append(f"【题目 {it.get('id')}】{it.get('problem', '')}")
    if len(cfg.get("items", [])) > 20:
        head.append(f"（共 {len(cfg['items'])} 道题，只列前 20 道）")
    if cfg.get("scope"):
        head.append(f"【口径】{cfg['scope']}")
    head.append(f"【现在】{'程序正在运行' if running else '程序没有在运行'}")
    states = feed.role_states(root, running) or {}
    names = feed.role_names(root)
    if states:
        head.append("【各角色状态】" + "；".join(
            f"{r}（{(names.get(r) or {}).get('name', r)}，{(names.get(r) or {}).get('kind', '')}）：{v.get('state')}"
            + (f"，已 {v.get('elapsed')} 秒" if v.get("elapsed") else "")
            + (f"，同时在做 {v['n']} 件事（" + "、".join(f"{x['label']} 已 {x['elapsed']} 秒" for x in v.get("calls", []))
               + "）" if v.get("n", 0) > 1 else "")
            + ("；" + "；".join(v["notes"]) if v.get("notes") else "") for r, v in states.items()))
    lines = []
    for m in feed.messages(root, 0, limit=100000)["messages"]:
        lines.append(f"#{m.get('seq')} {m.get('ts', '')[:19]} {m.get('speaker', '')}：{m.get('text', '')}")
    think = ""
    if running:
        lt = feed.live_thinking(root) or {}
        if lt:
            think = "\n".join(f"【{v.get('role')} {v.get('label', '')} 正在思考（最近一段）】\n{v.get('text', '')[-6000:]}"
                              for v in lt.values())
    return "\n".join(head), lines, think


def build_prompt(base, name, question, window_tokens, running):
    budget = max(4000, int(window_tokens or DEFAULT_WINDOW) - ANSWER_RESERVE - len(SYSTEM))
    head, lines, think = _material(base, name, running)
    conv = [f"{'用户' if h['who'] == '用户' else '解说者'}：{h['text']}" for h in history(base, name)]
    fixed = len(head) + len(think) + len(question) + 200
    room = budget - fixed
    # 先保证最近的对话，再放项目记录；都从最新往回放，放不下的最早的舍弃
    keep_conv, used = [], 0
    for c in reversed(conv):
        if used + len(c) > room * 0.3:
            break
        keep_conv.insert(0, c)
        used += len(c)
    keep_lines = []
    for ln in reversed(lines):
        if used + len(ln) > room:
            break
        keep_lines.insert(0, ln)
        used += len(ln)
    dropped = len(lines) - len(keep_lines)
    parts = [head]
    if lines:
        parts.append("【项目记录（从旧到新）】" + (f"\n（更早的记录已省略 {dropped} 条）" if dropped else "") +
                     "\n" + "\n".join(keep_lines))
    if think:
        parts.append(think)
    if keep_conv:
        parts.append("【之前和用户的对话】" + ("\n（更早的对话已省略）" if len(keep_conv) < len(conv) else "") +
                     "\n" + "\n".join(keep_conv))
    parts.append(f"【用户现在的问题】\n{question}")
    return "\n\n".join(parts), {"省略的记录条数": dropped, "预算字数": budget}


def ask(base: Path, name: str | None, question: str, settings: dict, secrets: dict, running: bool, hub=None) -> dict:
    from .models import ModelHub
    question = (question or "").strip()
    if not question:
        return {"ok": False, "msg": "内容是空的"}
    role, spec = find_role(settings)
    if not role:
        return {"ok": False, "msg": "还没有设置解说者。在 设置.json 的 roles 里加一个 kind 为「解说者」的角色（用便宜的模型就够了）。"}
    user, info = build_prompt(base, name, question, spec.get("context_window"), running)
    _append(base, name, {"who": "用户", "text": question, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    hub = hub or ModelHub(None, settings, secrets)
    try:
        text, meta = hub.chat_once(role, SYSTEM, user, max_tokens=int(spec.get("max_tokens") or 4000))
    except Exception as e:  # noqa: BLE001
        _append(base, name, {"who": "程序", "text": f"解说者没有回答：{e}", "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return {"ok": False, "msg": f"解说者没有回答：{e}"}
    rec = _append(base, name, {"who": "解说者", "text": text.strip(), "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "model": spec.get("model"), "served_model": meta.get("served_model"),
                               "seconds": meta.get("seconds"), "tokens_in": meta.get("tin"), "tokens_out": meta.get("tout"),
                               **info})
    return {"ok": True, "text": rec["text"]}
