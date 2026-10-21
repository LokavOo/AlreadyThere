"""2.9：插话（用户在运行中补充信息）与暂停（用户 2026-10-21 定）。2.9.1：打断按钮（一级打断／二级直接打断）、转折点
"永远先进审查者"——规则见 flow_form.py 开头。

规则：
- 用户的话是**用户指令**：发给所有角色（生产者、审查者、找茬者），模型照做；责任在用户。补充的内容从此成为题目的一部分，
  流程接着跑，不重来。账本里如实记下来源和时间；报告不另做区分。
- 运行中插话：程序不立刻打断，等正在跑的这一步全部返回后暂停；暂停中可以连着补充好几条，点「继续运行」后一起生效。
- 立刻停止是另一个按钮（app.stop，掐断所有调用），与这里无关。

进程之间怎么传话：流程跑在单独的进程里，账本（带哈希链）同一时刻只能有一个写入者。所以——
- 运行中：窗口把话写成项目文件夹里「插话待送达/」下的一个文件，并放一个「暂停请求」文件；流程在下一个检查点
  （每一步开始前）把待送达的话记进账本（"用户指令"，带生效位置），然后记"已暂停"并退出。
- 暂停中（没有进程在跑）：窗口直接把话记进账本，生效位置＝暂停的位置。
- 生效位置 pos＝(轮次, 步骤)：步骤 1＝生产者作答前，2＝审查前，3＝找茬前，4＝本轮小结前。位置在它之后（含）的调用都带上这条指令。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

INBOX = "插话待送达"
FLAG = "暂停请求"
STEPS = {1: "生产者作答前", 2: "审查前", 3: "找茬前", 4: "本轮小结前"}
MAX_LEN = 20000


class Paused(Exception):
    """流程在检查点按用户请求暂停。"""


class Voided(Exception):
    """2.9.1：直接打断切断的那一步里，没有存档的调用作废、不补。"""


def pos_text(pos):
    if not pos:
        return "流程开始前"
    r, s = pos
    return f"第 {r} 轮{STEPS.get(s, '')}"


# ---------------- 窗口这边 ----------------
def post(root: Path, text: str, request_pause: bool = True) -> dict:
    """运行中：写一条待送达的话，并请求暂停。"""
    root = Path(root)
    text = (text or "").strip()
    if not text:
        raise ValueError("内容是空的")
    if len(text) > MAX_LEN:
        raise ValueError(f"一次最多 {MAX_LEN} 字")
    d = root / INBOX
    d.mkdir(exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    n = len(list(d.glob("*.json"))) + 1
    f = d / f"{time.strftime('%Y%m%d%H%M%S')}_{time.time_ns() % 10**9:09d}_{n:03d}.json"
    f.write_text(json.dumps({"text": text, "ts": ts}, ensure_ascii=False), encoding="utf-8")
    if request_pause:
        (root / FLAG).write_text(ts, encoding="utf-8")
    return {"file": f.name, "ts": ts}


def pending(root: Path) -> list:
    d = Path(root) / INBOX
    out = []
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            out.append(dict(json.loads(f.read_text(encoding="utf-8")), file=f.name))
        except (OSError, ValueError):
            continue
    return out


def pause_requested(root: Path) -> bool:
    return (Path(root) / FLAG).exists()


def last_pause(project):
    """最后一次"已暂停"之后没有别的流程事件，就算暂停中；返回暂停的位置，否则 None。"""
    rows = project.q("SELECT type, content FROM events WHERE type NOT IN ('用户指令', '运行开始') ORDER BY seq DESC LIMIT 1")
    if rows and rows[0]["type"] == "已暂停":
        p = json.loads(rows[0]["content"]).get("位置")
        return tuple(p) if p else None
    return None


def log_instruction(project, text, ts, pos, how):
    n = len(project.q("SELECT 1 FROM events WHERE type='用户指令'")) + 1
    project.log("用户", "用户指令", round_=pos[0] if pos else None, ref_type="key", ref_id=f"U{n}",
                content={"编号": f"U{n}", "内容": text, "发出时间": ts, "位置": list(pos) if pos else None,
                         "生效": pos_text(pos), "送达方式": how})
    return f"U{n}"


def say_while_paused(project, text: str) -> str:
    """暂停中（没有进程在跑时由窗口调用）：直接记进账本，生效位置＝暂停的位置。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("内容是空的")
    if len(text) > MAX_LEN:
        raise ValueError(f"一次最多 {MAX_LEN} 字")
    pos = last_pause(project)
    if pos is None:
        raise ValueError("现在不在暂停中")
    return log_instruction(project, text, time.strftime("%Y-%m-%dT%H:%M:%S"), pos, "暂停中直接记入")


# ---------------- 流程这边 ----------------
def ingest(project, root: Path, pos) -> list:
    """把待送达的话记进账本（生效位置 pos），删掉文件。返回新编号。"""
    ids = []
    for m in pending(root):
        ids.append(log_instruction(project, m["text"], m.get("ts", ""), pos, "运行中插话，在检查点送达"))
        (Path(root) / INBOX / m["file"]).unlink(missing_ok=True)
    return ids


def load(project) -> list:
    out = []
    for r in project.q("SELECT content FROM events WHERE type='用户指令' ORDER BY seq"):
        c = json.loads(r["content"])
        out.append({"id": c["编号"], "text": c["内容"], "ts": c.get("发出时间", ""),
                    "pos": tuple(c["位置"]) if c.get("位置") else (1, 1)})
    return out


def load_cuts(project) -> list:
    """直接打断的记录：[(转折位置, 被切断的步骤)]。"""
    out = []
    for r in project.q("SELECT content FROM events WHERE type='直接打断' ORDER BY seq"):
        c = json.loads(r["content"])
        out.append((tuple(c["位置"]), tuple(c["切断的步骤"])))
    return out


STEP_OF = (("rv1-", 2), ("rvc-", 2), ("rv2-", 2), ("fc-", 3), ("rc-", 3), ("p-", 1))
FORM_PREFIX = ("f-", "fr-", "ff-", "frc-")


def step_of_key(key: str):
    """调用编号 → (轮次, 步骤)；表单阶段或认不出的返回 None。"""
    import re
    m = re.search(r"-r(\d+)(?:-u)?$", key or "")
    if not m or key.startswith(FORM_PREFIX):
        return None
    for pre, s in STEP_OF:
        if key.startswith(pre):
            return int(m.group(1)), s
    return None


def cut_position(step):
    """被切断的步骤 → 转折点：切在生产者那一步＝这一轮"先审"（已存档的算数，其余沿用已有答案）；
    切在审查或找茬那一步＝这一轮余下的不做，下一轮"先审"。"""
    r, s = step
    return (r, 1) if s == 1 else (r, s + 1)


def direct_interrupt(project, root: Path, inflight: list) -> dict:
    """窗口在掐断进程之后调用（此时只有窗口在写账本）：记"直接打断"，把待送达的话送达到转折点，记"已暂停"。"""
    steps = [step_of_key(k) for k in inflight]
    steps = [x for x in steps if x]
    if not steps:  # 正好不在等调用：按最后一次调用所在的步骤算
        row = project.q("SELECT call_id FROM calls ORDER BY ts DESC LIMIT 1")
        k = row[0]["call_id"].rsplit("#", 1)[0] if row else ""
        st = step_of_key(k)
        if st is None:
            raise ValueError("表单阶段（或还没开始做题）不支持直接打断，请用「停止运行」")
        steps = [st]
    step = max(steps)
    pos = cut_position(step)
    project.log("用户", "直接打断", round_=pos[0], content={"位置": list(pos), "切断的步骤": list(step),
                                                         "作废的调用": inflight,
                                                         "说明": "用户选择直接打断：正在跑的调用作废、不补"})
    ingest(project, root, pos)
    (Path(root) / FLAG).unlink(missing_ok=True)
    project.log("程序", "已暂停", round_=pos[0], content={"位置": list(pos), "说明": "已直接打断，正在跑的调用已作废。"
                                                                          "可以补充；补充完点「继续运行」接着做。"})
    return {"位置": pos, "作废": len(inflight)}


def block(instr) -> str:
    """附在提示词最前面的用户指令块（按提示词原则：只写事实和要做的事）。"""
    lines = ["【用户补充】以下内容由用户在流程进行中补充，是题目的一部分，按此执行："]
    for x in instr:
        lines.append(f"{x['id']}（{x['ts']}）：{x['text']}")
    return "\n".join(lines) + "\n\n"
