"""通用工具：哈希、时间、JSON 提取、数字归一。"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(obj) -> str:
    return sha256_text(canonical_json(obj))


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now_iso() -> str:
    """程序生成的时间戳（UTC，微秒精度）。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


class FormatError(ValueError):
    """模型返回内容不合规定格式（0.3 第 4 条的格式类失败）。"""


def extract_json(text: str):
    """从模型返回中取出 JSON 对象。优先取 ```json 代码块，否则取第一个完整的 {...}。"""
    if text is None:
        raise FormatError("返回为空")
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise FormatError("未找到可解析的 JSON 对象；请只输出一个 JSON 对象")


# ---------- 数字归一（0.2 第 5 条） ----------

_CN_DIGITS = {"零": 0, "〇": 0, "○": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_VAGUE = re.compile(r"(约|大约|左右|近|逾|余|多|大半|上下|若干|几)")


def cn_to_int(s: str):
    """把简单中文数字（含年份式逐位写法，如"一三二八"）转为整数；不认识返回 None。"""
    if not s:
        return None
    if all(ch in _CN_DIGITS for ch in s):  # 逐位写法
        return int("".join(str(_CN_DIGITS[ch]) for ch in s))
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + num) * unit
                total += section
                section = 0
            else:
                section += (num if num else 1) * unit
            num = 0
        else:
            return None
    return total + section + num


_NUM_RE = re.compile(r"(?P<num>-?\d+(?:\.\d+)?)\s*(?P<pct>%|％)?")
_CN_NUM_RE = re.compile(r"[零〇○一二两三四五六七八九十百千万]+")


def find_numbers(text: str):
    """返回文本中所有数字表达：[(原文, 归一值或 None)]。None 表示无法归一（须标出）。"""
    out = []
    if not text:
        return out
    for m in _NUM_RE.finditer(text):
        raw = m.group(0)
        try:
            v = Decimal(m.group("num"))
            if m.group("pct"):
                v = v / Decimal(100)
            ctx = text[max(0, m.start() - 2): m.start()]
            out.append((raw, None if _VAGUE.search(ctx) else v))
        except InvalidOperation:
            out.append((raw, None))
    for m in _CN_NUM_RE.finditer(text):
        raw = m.group(0)
        if len(raw) == 1 and raw in "一两":  # "一个""两者"之类，不计
            continue
        v = cn_to_int(raw)
        ctx = text[max(0, m.start() - 2): m.end() + 1]
        out.append((raw, None if (v is None or _VAGUE.search(ctx)) else Decimal(v)))
    return out


def written_matches_output(written: str, output_value) -> bool:
    """0.2 第 5 条：写出值等于输出值按写出值有效位数四舍五入后的值。"""
    try:
        w = Decimal(str(written))
        o = Decimal(str(output_value))
    except InvalidOperation:
        return False
    exp = w.as_tuple().exponent
    return o.quantize(Decimal(1).scaleb(exp)) == w


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")
