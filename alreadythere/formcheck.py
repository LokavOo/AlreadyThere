"""2.7 表单阶段的程序核对：只拆不改写。

用户 2026-10-03 定的原则："严在意思，宽在写法"。规则（详见迭代记录 2.7「逐字核对的松紧」）：
1. 只允许写法差异：空白与换行；LaTeX 与 Unicode 的同一符号（按下面的固定对照表）；公式定界符；全角与半角标点。
   统一之后，表单里的每一项必须与题面的某一段【完全一致】，不做任何模糊匹配。
2. 补词必须显式标出：拆开后要补主语或连接的词，放进【】里（例如"【G is】 an ℓ-uniform hypergraph"）。
   核对时先去掉【】里的内容再比对原文；【】里的词单独列出，标"含模型判断"。
   （说明：讨论时用的是方括号 [ ]，实现时改成【】，因为数学题里方括号常有含义，如 [2k-1]、区间 [a, b]。）
3. 关键词受保护：量词、否定词、比较词、数字和数学符号永远不能当连接词忽略；
   原文里没被任何一项覆盖的部分，只要含有可忽略连接词以外的东西，就算"漏了"。
4. 防整句照抄：一项里同时出现多个条件的迹象时，只提示"可能没拆开"，不拦下。
5. 核对不通过时由调用方按格式重试处理，不算答案的硬伤。

两张清单（写法对照表、可忽略连接词）都是程序里的固定清单；遇到新写法要加进清单并记入迭代记录，
不让模型临时认定"等价"。受保护的词不得进入可忽略清单（模块加载时自检）。
"""

from __future__ import annotations

import re
import unicodedata

FIELDS = ("条件", "范围", "所求", "完成标准", "排除项")
RANGES = ("相等", "更窄", "更宽")

# ---------------------------------------------------------------- 规则 1：写法对照表
# LaTeX 命令 → 统一写法。只收"同一个符号的不同写法"，不收任何会改变意思的替换。
LATEX_SYMBOLS = {
    r"\geqslant": "≥", r"\leqslant": "≤", r"\geq": "≥", r"\leq": "≤", r"\ge": "≥", r"\le": "≤",
    r"\neq": "≠", r"\ne": "≠", r"\ell": "ℓ", r"\infty": "∞", r"\cdot": "·", r"\times": "×",
    r"\notin": "∉", r"\in": "∈", r"\subseteq": "⊆", r"\supseteq": "⊇", r"\subset": "⊂", r"\supset": "⊃",
    r"\cup": "∪", r"\cap": "∩", r"\emptyset": "∅", r"\varnothing": "∅", r"\rightarrow": "→", r"\to": "→",
    r"\leftarrow": "←", r"\mapsto": "↦", r"\ldots": "…", r"\dots": "…", r"\cdots": "…", r"\mid": "|",
    r"\vert": "|", r"\lvert": "|", r"\rvert": "|", r"\forall": "∀", r"\exists": "∃", r"\neg": "¬",
    r"\land": "∧", r"\lor": "∨", r"\pm": "±", r"\approx": "≈", r"\equiv": "≡", r"\sim": "∼",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\epsilon": "ε", r"\varepsilon": "ε",
    r"\zeta": "ζ", r"\eta": "η", r"\theta": "θ", r"\kappa": "κ", r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν",
    r"\xi": "ξ", r"\pi": "π", r"\rho": "ρ", r"\sigma": "σ", r"\tau": "τ", r"\phi": "φ", r"\varphi": "φ",
    r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω", r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ",
    r"\Lambda": "Λ", r"\Sigma": "Σ", r"\Phi": "Φ", r"\Omega": "Ω",
}
# 只改字体、不改字母本身的命令：\mathcal{F} 与 F、ℱ 视为同一写法
FONT_CMDS = ("mathcal", "mathscr", "mathbf", "mathrm", "mathit", "boldsymbol", "bm", "text", "textrm",
             "textit", "operatorname", "mathsf")
SPACING_CMDS = (r"\left", r"\right", r"\big", r"\Big", r"\bigl", r"\bigr", r"\Bigl", r"\Bigr",
                r"\,", r"\;", r"\:", r"\!", r"\quad", r"\qquad", r"\ ", "~")
MATH_DELIMS = (r"\(", r"\)", r"\[", r"\]", "$")
EXTRA_CHARS = {"。": ".", "、": ",", "“": '"', "”": '"', "‘": "'", "’": "'", "－": "-", "−": "-", "–": "-", "—": "-"}

# ---------------------------------------------------------------- 规则 3：受保护的词与可忽略的连接词
PROTECTED_EN = ("all", "any", "every", "each", "some", "exist", "exists", "existence", "no", "not", "none", "nor",
                "only", "least", "most", "exactly", "more", "less", "than", "sufficiently", "large", "small",
                "infinitely", "finitely", "many", "few", "unique", "distinct", "maximum", "minimum", "max", "min",
                "maximal", "minimal", "always", "never", "or", "if", "unless", "except", "without", "nonzero",
                "positive", "negative", "nonnegative", "odd", "even", "prime", "such", "both", "either", "neither")
PROTECTED_ZH = ("所有", "任意", "任何", "每", "存在", "不", "非", "没有", "无", "至少", "至多", "恰好", "恰", "大于", "小于",
                "不超过", "不少于", "充分大", "足够大", "仅", "只", "唯一", "最大", "最小", "或", "若", "如果", "除")
PROTECTED_SYMBOLS = set("≥≤<>≠=∀∃¬∈∉⊆⊂⊇⊃+-×·/^|!∞")
GLUE_EN = ("let", "the", "a", "an", "of", "and", "to", "be", "is", "are", "was", "what", "find", "determine",
           "compute", "calculate", "give", "denote", "denotes", "where", "which", "that", "for", "as", "by", "we",
           "it", "its", "this", "these", "those", "then", "here", "there", "being", "with", "in", "on", "taken",
           "over", "value", "function")
GLUE_ZH = set("的和与及且设令记求试请问为是其中则")
GLUE_PUNCT = set(".,;:?!'\"()（）[]{}…")

_overlap = set(GLUE_EN) & set(PROTECTED_EN)
assert not _overlap, f"受保护的词不能进入可忽略清单：{_overlap}"
assert not (GLUE_ZH & set("".join(PROTECTED_ZH))), "受保护的中文字不能进入可忽略清单"


def _strip_font(s):
    # \mathcal{F} → F；嵌套一层就够用（题面里少见更深的嵌套）
    for c in FONT_CMDS:
        s = re.sub(r"\\" + c + r"\s*\{([^{}]*)\}", r"\1", s)
    return s


def normalize(s: str) -> str:
    """把一段文字统一成可比较的写法。只消除写法差异，不改意思。"""
    s = s or ""
    s = _strip_font(s)
    for k in sorted(LATEX_SYMBOLS, key=len, reverse=True):  # 先长后短：\geq 先于 \ge
        s = re.sub(re.escape(k) + r"(?![A-Za-z])", LATEX_SYMBOLS[k], s)
    for k in SPACING_CMDS:
        s = s.replace(k, " ")
    for k in MATH_DELIMS:
        s = s.replace(k, " ")
    s = s.replace("{", "").replace("}", "")
    for k, v in EXTRA_CHARS.items():
        s = s.replace(k, v)
    s = unicodedata.normalize("NFKC", s)  # 全角→半角、ℱ→F、ℓ→l、² →2 等
    s = re.sub(r"\s+", " ", s).strip()
    # 空格只在两边都是字母或数字时才保留（"k, ℓ ≥ 2" 与 "k,ℓ≥2" 一致）
    s = re.sub(r"(?<=[^A-Za-z0-9]) | (?=[^A-Za-z0-9])", "", s)
    return s


ADD_RE = re.compile(r"【([^】]*)】")


def split_added(text: str):
    """把【补词】拿出来：返回 (去掉补词后的文字, [补词…])。"""
    added = [x.strip() for x in ADD_RE.findall(text or "") if x.strip()]
    return ADD_RE.sub(" ", text or ""), added


def _find(S, F, used):
    """在统一后的题面 S 里找 F：优先找还没被别的项占用的那一处。返回 (起, 止) 或 None。"""
    if not F:
        return None
    starts = [m.start() for m in re.finditer(re.escape(F), S)]
    if not starts:
        return None
    for st in starts:
        if not any(used[st:st + len(F)]):
            return st, st + len(F)
    return starts[0], starts[0] + len(F)


_TOKEN = re.compile(r"[A-Za-z]+|[0-9]+|[一-鿿]|\S")


def _leftover_bad(text):
    """没被覆盖的一段里，除了可忽略的连接词和标点以外还剩什么。返回 (剩下的词, 其中受保护的词)。"""
    bad, prot = [], []
    for tok in _TOKEN.findall(text):
        low = tok.lower()
        if low in GLUE_EN or tok in GLUE_ZH or tok in GLUE_PUNCT:
            continue
        bad.append(tok)
        if low in PROTECTED_EN or tok in PROTECTED_SYMBOLS or tok.isdigit() or any(tok in w for w in PROTECTED_ZH):
            prot.append(tok)
    return bad, prot


_MULTI = (",", ";", " and ", "such that", " where ", " with ", "，", "；", "并且", "而且", "且")


def _multi_hint(text):
    t = f" {text} "
    n = sum(t.count(m) for m in _MULTI)
    return n >= 2


def check_form(source: str, form: dict) -> dict:
    """核对一份表单。返回 {"ok", "errors": [...], "hints": [...], "added": [...], "labels": [...], "spans": [...]}。
    errors 不为空＝不通过（由调用方退回重写）；hints 只提示、不拦下。labels 是统一后题面每个字符被归到的栏目。"""
    S = normalize(source)
    used = [None] * len(S)
    errors, hints, added, spans = [], [], [], []
    for i, it in enumerate(form.get("items") or [], 1):
        raw = it.get("text") or ""
        body, add = split_added(raw)
        F = normalize(body)
        for a in add:
            added.append({"项": i, "补词": a})
        if not F:
            errors.append(f"第 {i} 项只有补词、没有原文")
            continue
        pos = _find(S, F, used)
        if not pos:
            errors.append(f"第 {i} 项「{raw}」在题面原文里找不到（去掉【】补词、统一写法之后也找不到）。"
                          "表单只能用题面原文片段，不能改写；需要补的词放进【】里")
            continue
        a, b = pos
        for k in range(a, b):
            if used[k] is None:
                used[k] = it.get("field")
        spans.append({"项": i, "field": it.get("field"), "起": a, "止": b})
        if _multi_hint(body):
            hints.append(f"第 {i} 项「{raw}」里可能有不止一个条件（可能没拆开），请审查者判断")
    # 规则 3：没被覆盖的部分只允许是可忽略的连接词和标点
    k = 0
    while k < len(S):
        if used[k] is None:
            j = k
            while j < len(S) and used[j] is None:
                j += 1
            seg = S[k:j]
            bad, prot = _leftover_bad(seg)
            if bad:
                msg = f"题面里「{seg}」没有被任何一项覆盖（漏了）"
                if prot:
                    msg += f"，其中有不能省略的关键词：{'、'.join(dict.fromkeys(prot))}"
                errors.append(msg)
            k = j
        else:
            k += 1
    for i, t in enumerate(form.get("terms") or [], 1):
        if normalize(t.get("term") or "") not in S:
            errors.append(f"术语展开第 {i} 项的原词「{t.get('term')}」在题面原文里找不到")
    return {"ok": not errors, "errors": errors, "hints": hints, "added": added, "labels": used, "spans": spans,
            "source_norm": S}


def label_disagreements(source: str, results: dict) -> list:
    """几份表单的归类差异：results = {标签: check_form 的结果}。返回 [{"原文", "各方归类": {标签: 栏目}}]。
    只比两份都覆盖到的位置；写法统一之后按字符比较。"""
    S = normalize(source)
    labs = list(results)
    out = []
    k = 0
    while k < len(S):
        vals = {l: results[l]["labels"][k] if k < len(results[l]["labels"]) else None for l in labs}
        got = {l: v for l, v in vals.items() if v}
        if len(set(got.values())) > 1:
            j = k
            while j < len(S):
                v2 = {l: results[l]["labels"][j] if j < len(results[l]["labels"]) else None for l in labs}
                if v2 != vals:
                    break
                j += 1
            seg = S[k:j]
            if _leftover_bad(seg)[0]:  # 只是连接词或标点的分歧不算
                out.append({"原文": seg, "各方归类": got})
            k = j
        else:
            k += 1
    return out


# ---------------------------------------------------------------- 冻结后的表单 → 编号要求 → 给做题阶段看的文字

def requirements(form: dict) -> list:
    """把表单变成带编号的要求清单：[{"id": "1", "field", "text", "kind": "原文/隐含/术语"}]。
    排除项也编号（kind 照写），审查时不要求逐条给结论。"""
    out, n = [], 0
    for it in form.get("items") or []:
        n += 1
        out.append({"id": str(n), "field": it.get("field"), "text": it.get("text"), "zh": it.get("zh", ""),
                    "kind": "原文"})
    for it in form.get("implicit") or []:
        n += 1
        out.append({"id": str(n), "field": it.get("field"), "text": it.get("content"), "zh": "",
                    "kind": "隐含（含模型判断）", "reason": it.get("reason", "")})
    return out


def scope_text(form: dict) -> str:
    """做题阶段的口径＝表单里的完成标准和排除项。"""
    rq = requirements(form)
    done = [r["text"] for r in rq if r["field"] == "完成标准"]
    excl = [r["text"] for r in rq if r["field"] == "排除项"]
    s = "完成标准：" + ("；".join(done) if done else "（表单里没有写）")
    s += "。排除项（这道题不需要做的事）：" + ("；".join(excl) if excl else "（无）")
    return s


def render(form: dict) -> str:
    """冻结的表单 → 做题阶段的"题目"文字（2.7：做题阶段之后一律只依据表单，不再给原题）。"""
    lines = ["【题目表单（已冻结；以下各条就是这道题的全部要求，不要另外补充或改写题意）】"]
    for r in requirements(form):
        t = f"第{r['id']}条〔{r['field']}｜{r['kind']}〕{r['text']}"
        if r.get("zh"):
            t += f"（中文参考，仅供理解，以原文为准：{r['zh']}）"
        if r.get("reason"):
            t += f"（补这一条的理由：{r['reason']}）"
        lines.append(t)
    terms = form.get("terms") or []
    if terms:
        lines.append("【术语展开】")
        for t in terms:
            s = f"- {t.get('term')}：定义——{t.get('definition')}；实际要算——{t.get('compute')}；与完整定义相比：{t.get('range')}"
            if t.get("lost"):
                s += f"（丢掉了：{t['lost']}）"
            lines.append(s)
    return "\n".join(lines)


def excluded_ids(form: dict) -> set:
    return {r["id"] for r in requirements(form) if r["field"] == "排除项"}


def diff_forms(draft: dict, final: dict) -> list:
    """用户改动留痕：逐项比较模型起草版和冻结版。"""
    out = []
    for key, name in (("items", "原文项"), ("terms", "术语展开"), ("implicit", "隐含项")):
        a, b = draft.get(key) or [], final.get(key) or []
        for i in range(max(len(a), len(b))):
            x = a[i] if i < len(a) else None
            y = b[i] if i < len(b) else None
            if x != y:
                out.append({"位置": f"{name}第 {i + 1} 项", "起草": x, "冻结": y})
    return out
