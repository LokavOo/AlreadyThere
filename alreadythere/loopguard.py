"""2.11：检测思考（和回答）末尾的原样重复（俗称"死循环"），只用标准库，边收边查。

做法（用户 2026-10-21 定；八家模型意见的对照与整合见交接文件夹「互查_循环检测/整合方案_循环检测.md」）：
- 思考流、回答流各挂一个检测器，互不拼接；回答流门槛更高。
- 比较单位是"段"：遇换行或句末标点切一段（半角句点要后面跟空白才算），超过 SEG_MAX 字还没切分点就强制切一刀；
  比较前归一化（NFKC、大小写统一、连续空白合并），数字保留；只由符号组成的段（如 }、|---|、$$）不计。
- 主判定（会掐断）：末尾的若干段由同一块（一段或几段）原样连续重复构成，一直重复到最新收到的内容；
  另有一个字符级的短周期检查，专抓不换行、不带标点的复读（如 "wait wait wait"）。
- 二次确认：第一次命中只记"嫌疑"，之后每次检查都还在重复、并且又收了 confirm 个字，才算确认（真循环不会停，正常的重复——
  表格、代码、逐项清单——在这段时间里就结束了）。
- 两层只记录、不掐断（第一版，用户定）：只有数字在变的重复（数字换成 # 后原样重复）；新颖度过低
  （最近一段用 zlib 压缩后体积比例很小）。命中时记一条"疑似"，供账本和界面提示。
- 换说法兜圈子的"语义循环"用文字比对抓不到，这里不处理；也不另设"思考太久就掐"的上限（用户定）。
"""

from __future__ import annotations

import re
import unicodedata
import zlib

SEG_MAX = 200          # 没有切分点时，多长强制切一段
MAX_PERIOD = 48        # 最长检查多少段为一个周期
WINDOW = 400           # 只看最近多少段
_BREAK = set("\n。！？；!?;")
_SYMBOL_ONLY = re.compile(r"^[\W_]*$", re.UNICODE)
_DIGITS = re.compile(r"\d+")

DEFAULTS = {
    "thinking": {"confirm": 400, "min_cycles": 3, "min_cycles_p1": 8, "min_run": 200, "short_block": 8,
                 "short_cycles": 20, "masked_cycles": 12, "masked_run": 300, "stutter_run": 200},
    "answer": {"confirm": 800, "min_cycles": 4, "min_cycles_p1": 12, "min_run": 400, "short_block": 8,
               "short_cycles": 30, "masked_cycles": 20, "masked_run": 600, "stutter_run": 400},
}


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return " ".join(s.split())


class LoopFound(Exception):
    """确认了循环：调用方据此关闭连接。info 里有规则、重复的那一块、循环起点（原文字符位置）等。"""

    def __init__(self, channel: str, info: dict):
        super().__init__(f"疑似循环（{'思考' if channel == 'thinking' else '回答'}）：{info.get('rule')}")
        self.channel, self.info = channel, info
        self.think, self.text = "", ""   # 由调用方填上已收到的全部思考和回答（原文）


class Detector:
    """对一条流做增量检测。feed(增量) 返回 None 或确认的报告（dict）。"""

    def __init__(self, channel: str = "thinking", confirm: int | None = None, check_every: int = 64):
        if channel not in DEFAULTS:
            raise ValueError(channel)
        self.channel = channel
        self.p = dict(DEFAULTS[channel])
        if confirm is not None:
            self.p["confirm"] = int(confirm)
        self.check_every = check_every
        self.total = 0                # 已收到的原文字数
        self.raw_tail = ""            # 原文最近一段（字符级检查、新颖度用）
        self.buf = ""                 # 还没切成段的原文
        self.buf_start = 0            # buf[0] 在原文里的位置
        self.segs = []                # [(归一化文字, 原文起点, 原文长度)]
        self.since = 0
        self.pending = None           # 嫌疑：{"sig":…, "at": 首次命中时的 total, …}
        self.hit = None
        self.weak = []                # 只记录不掐断的"疑似"
        self._weak_kinds = set()

    # ---------- 切段 ----------
    def _cut(self):
        out = []
        i = 0
        b = self.buf
        start = 0
        while i < len(b):
            c = b[i]
            end = None
            if c in _BREAK:
                end = i + 1
            elif c == ".":
                if i + 1 >= len(b):
                    break
                if b[i + 1] in " \t\r\n":
                    end = i + 1
            elif i - start + 1 >= SEG_MAX:
                end = i + 1
            if end is not None:
                out.append((b[start:end], self.buf_start + start))
                start = end
            i += 1
        self.buf = b[start:]
        self.buf_start += start
        for raw, pos in out:
            n = normalize(raw)
            if not n or _SYMBOL_ONLY.match(n):
                continue
            self.segs.append((n, pos, len(raw)))
        if len(self.segs) > WINDOW:
            del self.segs[:len(self.segs) - WINDOW]

    def feed(self, delta: str):
        if self.hit or not delta:
            return self.hit
        self.total += len(delta)
        self.raw_tail = (self.raw_tail + delta)[-8192:]
        self.buf += delta
        self.since += len(delta)
        self._cut()
        if self.since >= self.check_every:
            self.since = 0
            self._check()
        return self.hit

    def finish(self):
        """流正常结束：只做一次弱信号检查（不再确认掐断）。"""
        return self.hit

    # ---------- 判定 ----------
    def _periodic(self, masked=False):
        segs = self.segs
        n = len(segs)
        if n < 2:
            return None
        keys = [_DIGITS.sub("#", x[0]) for x in segs] if masked else [x[0] for x in segs]
        key = keys.__getitem__
        p_ = self.p
        best = None
        for p in range(1, min(MAX_PERIOD, n // 2) + 1):
            t = 0
            while t + p < n and key(n - 1 - t) == key(n - 1 - t - p):
                t += 1
            L = t + p
            full = L // p
            if masked:
                need = p_["masked_cycles"]
            else:
                need = p_["min_cycles_p1"] if p == 1 else p_["min_cycles"]
            if full < need:
                continue
            start = n - L
            block_chars = sum(segs[i][2] for i in range(start, start + p))
            if not masked and block_chars < p_["short_block"] and full < p_["short_cycles"]:
                continue
            run = self.total - segs[start][1]
            if run < (p_["masked_run"] if masked else p_["min_run"]):
                continue
            if masked and all(key(i) == segs[i][0] for i in range(start, start + p)):
                continue  # 本身没有数字：那是原样重复，归主判定
            if best is None or L > best["segments"]:
                best = {"rule": ("只有数字在变的重复" if masked else "原样重复"), "period_segments": p,
                        "cycles": full, "segments": L, "start": segs[start][1],
                        "block": "".join(self._raw_seg(i) for i in range(start, start + p))[:500]}
        return best

    def _raw_seg(self, i):
        n, pos, ln = self.segs[i]
        off = pos - (self.total - len(self.raw_tail))
        if 0 <= off and off + ln <= len(self.raw_tail):
            return self.raw_tail[off:off + ln]
        return n

    def _stutter(self):
        """字符级短周期：末尾一段由长度 ≤40 的同一小块原样连续重复，总长 ≥ stutter_run。"""
        t = self.raw_tail
        need = self.p["stutter_run"]
        if len(t) < need:
            return None
        for p in range(1, 41):
            blk = t[-p:]
            if not blk.strip():
                continue
            k = need // p + 1
            if len(t) < p * k:
                continue
            if t[-p * k:] == blk * k:
                if _SYMBOL_ONLY.match(normalize(blk)) and "\n" not in t[-p * k:]:
                    continue  # 一行之内的纯符号（如 ----、====）不算
                c = k
                while len(t) >= p * (c + 1) and t[-p * (c + 1):-p * c] == blk:
                    c += 1
                return {"rule": "原样重复（不换行的短句复读）", "period_chars": p, "cycles": c,
                        "start": self.total - p * c, "block": blk}
        return None

    def _novelty(self):
        t = self.raw_tail[-4096:]
        if len(t) < 4096:
            return None
        b = t.encode("utf-8")
        r = len(zlib.compress(b, 6)) / len(b)
        if r < 0.06:
            return {"rule": "新颖度过低", "zlib_ratio": round(r, 3)}
        return None

    def _check(self):
        rep = self._periodic() or self._stutter()
        for w in (self._periodic(masked=True), self._novelty()):
            if w and w["rule"] not in self._weak_kinds:
                self._weak_kinds.add(w["rule"])
                self.weak.append(dict(w, at=self.total))
        if not rep:
            self.pending = None
            return
        # 从第一次命中起，每次检查都还在重复（规则可以不同：同一段复读先被字符级检查抓到、攒够段数后又被按段的检查抓到），
        # 中间只要有一次不重复就清掉嫌疑、重新计
        if self.pending is None:
            self.pending = {"at": self.total}
            return
        if self.total - self.pending["at"] >= self.p["confirm"]:
            self.hit = dict(rep, confirmed_at=self.total, suspect_at=self.pending["at"],
                            wasted=self.total - rep["start"], channel=self.channel)


class StreamGuard:
    """一次调用用一个：思考、回答各一个检测器。on_think/on_text 收增量，确认循环时抛 LoopFound。"""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.on = cfg.get("loop_guard", True) is not False
        self.think = Detector("thinking", cfg.get("loop_confirm_chars"))
        self.text = Detector("answer", cfg.get("loop_confirm_chars_answer"))

    def on_think(self, d):
        if self.on and self.think.feed(d):
            raise LoopFound("thinking", self.think.hit)

    def on_text(self, d):
        if self.on and self.text.feed(d):
            raise LoopFound("answer", self.text.hit)

    def weak(self):
        out = []
        for det, ch in ((self.think, "思考"), (self.text, "回答")):
            out += [dict(w, channel=ch) for w in det.weak]
        return out


def scan(text: str, channel: str = "thinking", chunk: int = 16, confirm: int | None = None):
    """测试和截断复查用：把一段文字按 chunk 字一块喂进检测器，返回 (确认的报告或 None, 弱信号列表)。"""
    d = Detector(channel, confirm)
    for i in range(0, len(text), chunk):
        if d.feed(text[i:i + chunk]):
            break
    if not d.hit:
        d.since = d.check_every
        d._check()
        if d.pending and not d.hit and confirm == 0:
            d._check()
    return d.hit, d.weak


def trim(think: str, start: int, rounds: int = 3) -> str:
    """从循环第一次出现的位置整段切掉，退到最近的句末；切完再查一遍（不要确认期），还有就再切，最多 rounds 次。"""
    cut = max(0, min(start, len(think)))
    for _ in range(rounds):
        head = think[:cut]
        pos = max(head.rfind(c) for c in "\n。！？；!?;.")
        head = head[:pos + 1] if pos > 0 else head
        hit, _ = scan(head, "thinking", chunk=64, confirm=0)
        if not hit or hit["start"] >= len(head):
            return head.rstrip()
        cut = hit["start"]
    return think[:cut].rstrip()
