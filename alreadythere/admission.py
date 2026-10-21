"""建题窗口背后的逻辑（2.1，数学最终答案题第一版）：导入 → 字段对应 → 程序自检 → 生成题目文件。

只用标准库。自检是确定性的程序检查（不调用模型）：
红＝几乎肯定有问题，提交前必须逐条点"我已知道"；黄＝提醒，可直接提交；绿＝没发现问题。
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from .fields.answer import as_number, normalize

GUESS = {
    "problem": ("problem", "question", "prompt", "题目", "题面", "问题", "text"),
    "gold": ("answer", "gold", "final_answer", "标准答案", "答案", "solution_answer", "target"),
    "id": ("id", "problem_idx", "idx", "uid", "编号", "序号"),
    "name": ("name", "title", "标题", "名称", "category", "type"),
}
MAX_PROBLEM_CHARS = 6000
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


def read_table(text: str, filename: str):
    """从文本读出题目行（list[dict]）。按扩展名识别 csv / json / jsonl。"""
    ext = Path(filename).suffix.lower()
    text = text.lstrip("\ufeff")
    if ext == ".csv":
        rows = list(csv.DictReader(io.StringIO(text)))
    elif ext == ".jsonl":
        rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    elif ext == ".json":
        obj = json.loads(text)
        if isinstance(obj, dict):
            for k in ("items", "data", "problems", "questions", "rows"):
                if isinstance(obj.get(k), list):
                    obj = obj[k]
                    break
            else:
                raise ValueError("JSON 是对象，但里面找不到题目列表（items/data/problems/questions/rows）")
        rows = obj
    else:
        raise ValueError(f"不支持的文件类型 {ext or '(无扩展名)'}：请用 csv、json 或 jsonl（parquet 先用转换脚本转成 jsonl）")
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        raise ValueError("文件里没有读到任何题目行")
    return rows


def columns_of(rows):
    cols = []
    for r in rows[:50]:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols


def guess_mapping(cols):
    low = {c.lower(): c for c in cols}
    out = {}
    for field, names in GUESS.items():
        out[field] = next((low[n.lower()] for n in names if n.lower() in low), None)
    return out


def _s(v):
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


def to_items(rows, mapping, prefix="Q"):
    """按字段对应生成条目；编号缺失或重复时自动编号。"""
    items, used = [], set()
    for i, r in enumerate(rows, 1):
        iid = _s(r.get(mapping.get("id"))) if mapping.get("id") else ""
        iid = re.sub(r"[^A-Za-z0-9_-]", "_", iid)[:24] if iid else ""
        iid = f"{prefix}{iid}" if iid and not iid.startswith(prefix) else iid
        if not iid or iid in used:
            n = i
            iid = f"{prefix}{n:03d}"
            while iid in used:  # 自动编号也不能和已有的撞
                n += 1
                iid = f"{prefix}{n:03d}"
        used.add(iid)
        items.append({"id": iid, "name": _s(r.get(mapping.get("name"))) if mapping.get("name") else "",
                      "problem": _s(r.get(mapping.get("problem"))), "gold": _s(r.get(mapping.get("gold")))})
    return items


def self_check(items):
    """返回 [{"id","level","code","msg"}]。level: 红/黄。没有问题的条目不出现（即绿）。"""
    out, seen = [], {}
    for it in items:
        iid, prob, gold = it["id"], it.get("problem", ""), it.get("gold", "")

        def add(level, code, msg):
            out.append({"id": iid, "level": level, "code": code, "msg": msg})
        if not prob.strip():
            add("红", "no_problem", "题面是空的")
        if not gold.strip():
            add("红", "no_gold", "没有标准答案，无法比对对错")
        key = re.sub(r"\s+", "", prob)
        if key and key in seen:
            add("红", "dup", f"题面与 {seen[key]} 完全相同")
        elif key:
            seen[key] = iid
        if len(prob) > MAX_PROBLEM_CHARS:
            add("黄", "long", f"题面很长（{len(prob)} 字），可能含多道题或附带了解答")
        if gold.strip():
            g = normalize(gold)
            if as_number(g) is None:
                add("黄", "gold_not_number", "标准答案不是纯数字：与模型答案形式不同时，会列入"
                                          "待人工判断，需要你来判定")
            if len(gold) > 120:
                add("黄", "gold_long", "标准答案很长，像是整段解答而不是最终答案")
            if prob and g and len(g) >= 3 and g in re.sub(r"\s+", "", prob):
                add("黄", "gold_in_problem", "标准答案的文字出现在题面里，题面可能泄露了答案")
        if re.search(r"(?i)\b(prove|show that)\b|证明|求证", prob):
            add("黄", "proof", "像是证明题：最终答案题不适合证明题（没有唯一的最终答案）")
        if re.search(r"(?i)\b(true or false|multiple choice)\b|选择题|\([A-D]\)\s", prob):
            add("黄", "choice", "像是选择题：建议改成开放题（去掉选项让模型直接作答）")
    return out


def summary(checks, n):
    red = {c["id"] for c in checks if c["level"] == "红"}
    yellow = {c["id"] for c in checks if c["level"] == "黄"} - red
    return {"总数": n, "红": len(red), "黄": len(yellow), "绿": n - len(red) - len(yellow)}


def build_config(project_id, question, scope, items, *, source="", license_="", acknowledged=(), params=None):
    """生成题目文件内容。有红色问题且没被逐条确认的，拒绝生成。"""
    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
        raise ValueError("题目列表格式不对")
    seen_ids = set()
    for x in items:
        iid = x.get("id")
        if not isinstance(iid, str) or not ID_RE.match(iid):
            raise ValueError(f"题目编号只能用英文字母、数字、下划线或短横线（最长 24 个）：{iid!r}")
        if iid in seen_ids:
            raise ValueError(f"题目编号重复：{iid}")
        seen_ids.add(iid)
        for k in ("name", "problem", "gold"):
            if not isinstance(x.get(k, ""), str):
                raise ValueError(f"{iid} 的 {k} 必须是文字")
            x.setdefault(k, "")
    checks = self_check(items)
    ack = set(a for a in acknowledged if isinstance(a, str))
    unacked = sorted({c["id"] for c in checks if c["level"] == "红"} - ack)
    if unacked:
        raise ValueError(f"这些题有红色问题，需要逐条点「我已知道」，或先剔除：{'、'.join(unacked[:10])}")
    if not re.match(r"^[A-Za-z0-9_-]{1,32}$", project_id or ""):
        raise ValueError("项目编号只能用英文字母、数字、下划线或短横线（最长 32 个）")
    if not items:
        raise ValueError("没有题目")
    cfg = {"project_id": project_id, "task": "final_answer", "question": question, "scope": scope,
           "items": items, "admission": {"来源": source, "许可": license_, "自检": checks,
                                         "已知晓的红色问题": sorted(ack), "题型": "数学最终答案题"}}
    if params:
        cfg["params"] = params
    return cfg


def write_question_file(base: Path, cfg: dict) -> Path:
    path = Path(base) / f"{cfg['project_id']}.json"
    if path.exists():
        raise FileExistsError(f"已经有叫 {path.name} 的题目文件了，换个项目编号")
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
