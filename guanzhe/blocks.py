"""产物块格式（0.4）与提交检查（第 6 节第 3 步）。

模型提交的产物是一个 JSON 对象：
{
  "blocks":   [ {块}, ... ],      # 需检查的内容只能写在块里
  "notes":    "说明文字",          # 衔接与解释，不承载主张
  "changes":  [ {类型, 条目, 说明}, ... ],   # 修改清单（第 2 轮起）
  "responses":[ {意见编号, 处理: 采纳/驳回, 核验记录编号, 理由}, ... ]
}
块编号由程序分配（0.4.4）。块中引用的检索编号必须存在，原句必须出现在该检索的存档原文中。
"""

from __future__ import annotations

import re

from .util import FormatError, find_numbers, normalize_ws

VERIFY_CLAIM_WORDS = ("已验证", "已核对", "已确认", "经核实", "核实无误")

# 状态维度取值（0.5），从高到低
CHECK_ORDER = ["已查证", "已查证（含模型判断）", "未查证", "出处无法解析"]
VERIFY_ORDER = ["已验证", "待核验", "未验证"]
INDEP_ORDER = ["独立", "同方法独立实现", "独立性未确认", "未独立验证", "疑似非独立待查", "确认非独立"]


def lowest(order, values):
    vals = [v for v in values if v in order]
    return max(vals, key=order.index) if vals else None


def validate_product(obj, *, block_schema: dict, item_ids: set, require_changes: bool):
    """格式检查。不合格抛 FormatError（触发格式类重试）。block_schema: 块类型 -> 必填字段列表。"""
    if not isinstance(obj.get("blocks"), list) or not obj["blocks"]:
        raise FormatError("缺少 blocks 数组或为空")
    for i, b in enumerate(obj["blocks"]):
        if not isinstance(b, dict):
            raise FormatError(f"第 {i + 1} 个块不是 JSON 对象")
        t = b.get("type")
        if t not in block_schema:
            raise FormatError(f"第 {i + 1} 个块的 type={t!r} 不是允许的块类型：{list(block_schema)}")
        if b.get("item") not in item_ids:
            raise FormatError(f"第 {i + 1} 个块的 item={b.get('item')!r} 不是已登记的条目编号")
        fields = b.get("fields")
        if not isinstance(fields, dict):
            raise FormatError(f"第 {i + 1} 个块缺少 fields 对象")
        missing = [f for f in block_schema[t] if f not in fields or fields[f] in (None, "")]
        if missing:
            raise FormatError(f"第 {i + 1} 个块（条目 {b.get('item')}）缺少必填字段：{missing}")
    if require_changes and not isinstance(obj.get("changes"), list):
        raise FormatError("第 2 轮起须提交修改清单 changes（没有改动时提交空数组 []）")
    for key in ("changes", "responses"):
        if obj.get(key) is not None and not isinstance(obj.get(key), list):
            raise FormatError(f"{key} 须是数组")
        for x in obj.get(key) or []:
            if not isinstance(x, dict):
                raise FormatError(f"{key} 的每一项须是 JSON 对象")
    for c in obj.get("changes", []) or []:
        if c.get("type") not in ("数值", "判据", "结论", "措辞"):
            raise FormatError(f"修改清单项类型 {c.get('type')!r} 不合规，只能是 数值/判据/结论/措辞")
    for r in obj.get("responses", []) or []:
        if r.get("action") not in ("采纳", "驳回"):
            raise FormatError("responses 中 action 只能是 采纳 或 驳回")


def locate_quote(archive_text: str, quote: str, ctx_chars: int = 300):
    """在存档原文中找原句（1.4）：先去掉网页排版符号（链接、表格竖线等），再忽略空白、标点与繁简差异比对。
    模型看到的摘录也是这份去排版文本，所以"照着摘录抄"一定能对上。
    返回 (是否找到, 在去排版文本中的偏移, 原句所在的那一段, 前后文)。"""
    from .textnorm import clean_md, find_skeleton
    if not archive_text or not quote:
        return False, None, None, None
    text = clean_md(archive_text)
    span = find_skeleton(text, clean_md(quote))  # 模型有时把原文里的排版符号一起抄进原句，同样去掉
    if not span:
        return False, None, None, None
    a, b = span
    ps = text.rfind("\n", 0, a) + 1
    pe = text.find("\n", b)
    pe = len(text) if pe < 0 else pe
    ctx = text[max(0, a - ctx_chars): min(len(text), b + ctx_chars)]
    return True, a, text[ps:pe], ctx


def numbers_in(text):
    return {v for _, v in find_numbers(text) if v is not None}


def flag_notes(notes: str):
    flags = []
    if not notes:
        return flags
    notes = str(notes)
    # 1.5：检索编号（S00123）、条目编号（I05）、块编号（P1-R2-B003）等不是数字，先去掉再找
    plain = re.sub(r"(?<![A-Za-z0-9])(?:[SIAEO]\d{2,}|[PR]\d+-R\d+-B\d+|R\d+-[A-Za-z0-9-]+)(?![A-Za-z0-9])", " ", notes)
    nums = [raw.strip() for raw, _ in find_numbers(plain)]
    if nums:
        flags.append(f"说明文字中出现数字：{nums[:10]}")
    claims = [w for w in VERIFY_CLAIM_WORDS if w in notes]
    if claims:
        flags.append(f"说明文字中出现验证声明：{claims}（验证状态只由程序给出）")
    return flags
