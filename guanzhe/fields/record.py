"""人名名单、引文两种核对器，以及把几个字段合成一个条目的"组合"核对器（1.8，诺贝尔奖题起用）。

与日期核对器（date.py）同一套接口，主流程不用区分字段类型：
check / value / normalize / agree / format / is_partial / review_values / equivalents / evidence / matches /
category / category_of_evidence / stall_keys / noun。
"""

from __future__ import annotations

import re
import unicodedata

from ..textnorm import clean_md, skeleton

_SEP = re.compile(r"\s*[；;|、]\s*|\s*\n\s*")


_TITLES = {"sir", "dame", "lord", "jr", "sr", "dr", "prof"}  # 称号、Jr. 之类不影响比对


def fold_latin(s: str) -> str:
    """西文名字规整：去变音符号、转小写、标点换空格、去掉括号里的缩写和单个字母（中间名缩写）。"""
    s = re.sub(r"[（(][^）)]*[）)]", " ", s or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ß", "ss").replace("ı", "i").replace("ø", "o").replace("æ", "ae").replace("đ", "d").replace("ł", "l")
    s = re.sub(r"[^0-9a-z一-鿿]+", " ", s)
    return " ".join(t for t in s.split() if (len(t) > 1 or not t.isascii()) and t not in _TITLES)


def split_names(text: str):
    return [x.strip() for x in _SEP.split(text or "") if x.strip()]


class NameListField:
    """得主名单：填原文（西文）名字，多个用"；"隔开。每个名字（规整后）须出现在所引原文里；
    两方一致 = 规整后的名字集合相同（顺序、变音符号、大小写、中间名缩写不影响）。"""

    kind = "人名名单"

    def __init__(self, key="得主"):
        self.key = key

    def check(self, f, raw):
        names = split_names(f.get(self.key))
        if not names:
            return [f"没有填{self.key}"], "未查证"
        body = " " + fold_latin(clean_md(raw)) + " "
        miss = [n for n in names if not fold_latin(n) or f" {fold_latin(n)} " not in body]
        if miss:
            return [f"{self.key}「{'、'.join(miss)}」未在所注原文中找到（须照原文写原名，多个用；隔开）"], "未查证"
        return [], "待审查"

    def norm(self, text):
        names = split_names(text)
        return frozenset(fold_latin(n) for n in names) if names else None

    def found_in(self, text, evidence):
        return all(f" {n} " in " " + fold_latin(evidence) + " " for n in (self.norm(text) or ()))


_PARTS = re.compile(r"\s*[；;]\s*|\s*\n\s*")


class CitationField:
    """获奖原因：须逐字引用原文（忽略大小写、标点、空白、开头的 for）。1.9：一个奖分几部分颁发、各有各的获奖原因时，
    分几段填，用"；"隔开，每段都须在原文里逐字找到。两方一致 = 各段一一对应（顺序不论）；每段规整后相同，
    或一方包含另一方且短的至少是长的八成（官网不同页面偶有多一个从句）。"""

    kind = "引文"

    def __init__(self, key="获奖原因"):
        self.key = key

    @staticmethod
    def sk(text):
        t = unicodedata.normalize("NFKD", text or "")
        t = "".join(c for c in t if not unicodedata.combining(c)).lower()
        t = skeleton(t)
        return t[3:] if t.startswith("for") else t

    def parts(self, text):
        """分段后的规整写法（元组）；整句本身就能在原文里找到时，调用方会按一段处理。"""
        ps = tuple(self.sk(x) for x in _PARTS.split(text or "") if self.sk(x))
        return ps

    def check(self, f, raw):
        whole = self.sk(f.get(self.key))
        if len(whole) < 8:
            return [f"没有填{self.key}，或太短"], "未查证"
        src = self.sk(clean_md(raw))
        if whole in src:
            return [], "待审查"
        miss = [x for x in _PARTS.split(f.get(self.key) or "") if self.sk(x) and self.sk(x) not in src]
        if miss or len(self.parts(f.get(self.key))) < 2:
            return [f"{self.key}未在所注原文中找到（须从原文逐字引用官方表述；一个奖分几部分颁发、各有获奖原因的，"
                    f"分段填、用；隔开）"], "未查证"
        return [], "待审查"

    def norm(self, text, raw_ok=True):
        return self.parts(text)

    def same_set(self, a, b):
        """两组分段是否一一对应（顺序不论）。"""
        if len(a) != len(b):
            if len(a) == 1 or len(b) == 1:  # 一方照抄了整句（含"另一半授予…"），另一方分段：各段都在那一整句里即算一致
                one, many = (a[0], b) if len(a) == 1 else (b[0], a)
                return all(x in one for x in many) and sum(len(x) for x in many) >= 0.3 * len(one)
            return False
        rest = list(b)
        for x in a:
            m = next((y for y in rest if self.same(x, y)), None)
            if m is None:
                return False
            rest.remove(m)
        return True

    def same(self, a, b):
        if a == b:
            return True
        s, l_ = sorted((a, b), key=len)
        return bool(s) and s in l_ and len(s) >= 0.8 * len(l_)


class Rec:
    """规整后的一条记录：比对用规整后的值，显示用原来的写法。"""

    def __init__(self, names, cit, names_txt, cit_txt):
        self.names, self.cit, self.names_txt, self.cit_txt = names, cit, names_txt, cit_txt

    def __bool__(self):
        return True

    def __repr__(self):
        return f"Rec({sorted(self.names)!r}, {[x[:20] for x in self.cit]!r})"


class RecordField:
    """诺贝尔奖题的条目：得主（人名名单）＋获奖原因（引文），两项都核对通过才算通过；另有"中文参考"一栏
    （模型翻译，仅供参考，程序不核对）。"""

    kind = "记录"
    equivalents_label = "程序规整后的写法"
    SEP = " ｜ 获奖原因："

    def __init__(self, names=None, citation=None, noun="获奖", note_key="中文参考"):
        self.N = names or NameListField()
        self.C = citation or CitationField()
        self.noun, self.note_key = noun, note_key
        self.key = f"{self.N.key}与{self.C.key}"

    # ---------- 基本 ----------
    def value(self, fields):
        fields = fields or {}
        n, c = (fields.get(self.N.key) or "").strip(), (fields.get(self.C.key) or "").strip()
        return f"{self.N.key}：{n}{self.SEP}{c}" if (n or c) else None

    def _parts(self, value):
        v = str(value or "")
        if self.SEP not in v:
            return None, None
        a, b = v.split(self.SEP, 1)
        return a.split("：", 1)[-1].strip(), b.strip()

    def normalize(self, value):
        n, c = self._parts(value)
        names = self.N.norm(n)
        cit = self.C.parts(c)
        if not names or sum(len(x) for x in cit) < 8:
            return None
        return Rec(names, cit, "；".join(split_names(n)), c)

    def format(self, d):
        return f"{self.N.key}：{d.names_txt}{self.SEP}{d.cit_txt}"

    def is_partial(self, d):
        return False

    def agree(self, recs):
        recs = [r for r in recs if r]
        if not recs:
            return None
        first = recs[0]
        if all(r.names == first.names and self.C.same_set(r.cit, first.cit) for r in recs):
            return max(recs, key=lambda r: sum(len(x) for x in r.cit))
        return None

    def stall_keys(self):
        return (self.N.key, self.C.key)

    # ---------- 提交检查 ----------
    def check(self, f, raw, hint=None):
        order = ["出处无法解析", "未查证", "待审查"]
        flags, st = [], "待审查"
        for sub in (self.N, self.C):
            fl, s = sub.check(f, raw)
            flags += fl
            if order.index(s) < order.index(st):
                st = s
        if f.get(self.note_key):
            flags.append(f"{self.note_key}为模型翻译，仅供参考，程序未核对")
        return flags, st

    # ---------- 审查 ----------
    def review_values(self, f):
        return {self.N.key: f.get(self.N.key), self.C.key: f.get(self.C.key)}

    def equivalents(self, value, hint=None):
        d = self.normalize(value)
        if not d:
            return None
        return {f"{self.N.key}（规整后，比对用）": sorted(d.names),
                "说明": "程序比对名字时忽略大小写、变音符号、中间名缩写；比对获奖原因时忽略大小写、标点和开头的 for"}

    def evidence(self, text, hint=None):
        return clean_md(text or "") or None

    def matches(self, value, evidence):
        d = self.normalize(value)
        if not d or not evidence:
            return False
        ev = self.C.sk(evidence)
        return self.N.found_in(d.names_txt, evidence) and all(x in ev for x in d.cit)

    # ---------- 分类统计（本题不用） ----------
    def category(self, fields_list, d, hint=None):
        return None

    def category_of_evidence(self, x, hint=None):
        return None

    def display(self, f):
        return f"{self.N.key}：{f.get(self.N.key) or '—'}\n{self.C.key}：{f.get(self.C.key) or '—'}" + (
            f"\n{self.note_key}（模型翻译，未核对）：{f.get(self.note_key)}" if f.get(self.note_key) else "")
