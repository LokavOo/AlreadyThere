"""2.7：数学题两段式流程的提示词与校验。

第一段（表单阶段）：生产者把题面拆成表单（只拆不改写），审查者一次看全部表单比对，找茬者按出场规则出场。
第二段（做题阶段）：生产者只看冻结的表单做题；审查者"分开审、再合起来对照"——每份答卷单独审一次（逐条审查、
额外前提、反驳够用即止、写摘要），再做一次轻量对照（只看自己写的摘要，查材料间冲突、判断路线是否相同）；
找茬者看完整答卷找共用前提和冲突（找到即止），意见只给审查者，由审查者复查后决定是否采纳。
另有"裸题对照"：只给题目本身，看模型在不加任何包装时答得怎样。
"""

from __future__ import annotations

import json
import re

from .. import formcheck as FC
from ..util import FormatError
from .final_answer import BASES, LABELS, _norm_basis, _s, lang_block, libs_line, scope_block, sub_block  # noqa: F401

FORM_BASES = ("遗漏", "多余", "改了意思", "归类错误", "切分不当", "术语展开不当", "隐含项不当")


def _ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def quote_in(quote, text):
    """摘要/引文核对："只摘不改"——引文必须逐字出现在原文里（只忽略空白差异和写法差异）。"""
    q = _ws(quote)
    if not q:
        return False
    if q in _ws(text):
        return True
    return FC.normalize(q) in FC.normalize(text)


# ================================================================ 第一段：表单

SYSTEM_FORM = """你是多模型互查流程中的一名【生产者】，这一步不做题，只把题面拆成一张表单。
你看不到其他生产者。表单会给审查者比对，确认后冻结，之后做题的人【只看这张表单，看不到原题】，所以不能漏、不能加、不能改意思。
把题目看成"在 A 且 B 且非 C 的情况下求 D"：找出其中的每一项。规则：
1. 【只拆，不改写】每一项的 text 必须是题面原文的一个片段，逐字照抄（公式可以用 LaTeX 或 Unicode，两种写法程序都认；空格、换行不要紧）。
   不要换成自己的话，不要总结，不要解释，不要翻译术语（如 covering number 就保留原词），不要把题目改成你熟悉的标准形式。
2. 一项只放一个条件。拆开后句子缺主语或连接词、非补不可的，把补的词放进【】里，例如"【G is】 an ℓ-uniform hypergraph"。【】里的词会被单独列出、标为"含模型判断"，没必要就不要补。
3. field 五选一：条件（题目给定的前提）／范围（量词、取值范围、在什么范围里取最大最小等，如 "for all…"、"over all pairs…"、"for sufficiently large k"）／所求（要求的是什么）／完成标准（做到什么程度算完成，如只要最终式子、要证明）／排除项（这道题不需要做的事）。
   题面里所有内容都要归到某一项里，除了 let、the、of、what is 这类连接词和标点。量词、否定词、比较词、数字、符号一个都不能漏。
4. 完成标准和排除项也从题面拆：题面写了就照抄原文片段；题面没写但非有不可的（例如题面只问"What is the exact value"，没说要不要证明），写进 implicit（隐含项），不要编进 items。
5. terms（术语展开）：题面里需要落实成具体操作的关键术语，每个写一条：term＝原词（逐字照抄）；definition＝它的完整定义；compute＝实际要算的东西；range＝与完整定义相比是 相等／更窄／更宽；range 不是相等时 lost 写明丢掉了什么。不要为了省事把术语缩小（例如把 k 阶正规简化成 1 阶）。
6. implicit（隐含项）：题面没写、但你认为非补不可的东西。每条写 field、content、reason（为什么非补不可）。没有必要就不要加：隐含项是唯一可能"加东西"的地方，只要有一方补了而别人没补，就会被标为有争议。
7. zh（可选）：每项可以附一句中文翻译，只供人参考，不核对，也不给做题的人当依据。
8. 不要解题，不要评价题目难不难，不要写答案。只输出一个 JSON 对象。"""

SCHEMA_FORM = ('{"items": [{"field": "条件 / 范围 / 所求 / 完成标准 / 排除项", "text": "题面原文片段（补词放【】里）", "zh": "（可选）中文参考"}], '
               '"terms": [{"term": "原词", "definition": "完整定义", "compute": "实际要算的东西", "range": "相等 / 更窄 / 更宽", "lost": "（range 不是相等时必填）"}], '
               '"implicit": [{"field": "…", "content": "补的内容", "reason": "为什么非补不可"}]}\n'
               '说明：terms、implicit 没有就写 []。')

SYSTEM_FORM_REVISE = SYSTEM_FORM + """
9. 这是修改轮：审查者（或程序）给了你一些意见。【先核实，再决定】：每条意见对照题面原文核实后写采纳或驳回，check 写清楚你怎么核实的。然后给出修改后的完整表单。
10. 每条意见都必须有一条 responses，opinion 填意见编号。"""

SCHEMA_FORM_REVISE = ('{"responses": [{"opinion": "意见编号", "decision": "采纳 或 驳回", "check": "你是怎么核实的"}], '
                      '"items": [...同上...], "terms": [...], "implicit": [...]}')


def form_prompt(problem, lang=None):
    return (f"题面原文：\n{problem}\n{lang_block(lang)}\n\n按下面的格式输出 JSON：\n{SCHEMA_FORM}")


def form_text(form, label=None):
    """一份表单的文字写法（给审查者、给生产者自己的历史、给人看都用这个）。"""
    p = []
    for i, it in enumerate(form.get("items") or [], 1):
        p.append(f"  {i}.〔{it.get('field')}〕{it.get('text')}" + (f"（中文参考：{it['zh']}）" if it.get("zh") else ""))
    for t in form.get("terms") or []:
        p.append(f"  术语：{t.get('term')}｜定义：{t.get('definition')}｜实际要算：{t.get('compute')}｜范围比较：{t.get('range')}"
                 + (f"｜丢掉了：{t['lost']}" if t.get("lost") else ""))
    for h in form.get("implicit") or []:
        p.append(f"  隐含项〔{h.get('field')}〕{h.get('content')}｜理由：{h.get('reason')}")
    return (f"【生产者{label}的表单】\n" if label else "") + "\n".join(p)


def form_revise_prompt(problem, prev_form, ops, lang=None):
    p = [f"题面原文：\n{problem}\n{lang_block(lang)}", "\n你上一轮的表单：\n" + form_text(prev_form),
         "\n现在需要你逐条回应的意见："]
    for o in ops:
        p.append(f"- 意见编号：{o['id']}\n  严重程度：{o['severity']}" + (f"（依据：{o['basis']}）" if o.get("basis") else "")
                 + f"\n  内容：{o['problem']}")
    p.append(f"\n逐条核实后，按下面的格式输出 JSON：\n{SCHEMA_FORM_REVISE}")
    return "\n".join(p)


def _clean_form(obj):
    if not isinstance(obj, dict):
        raise FormatError("返回的不是 JSON 对象")
    items = obj.get("items")
    if not isinstance(items, list) or not items:
        raise FormatError("缺少 items（表单的各项），至少要有一项")
    out = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise FormatError(f"items 第 {i} 项不是对象")
        f = _s(it.get("field"))
        if f not in FC.FIELDS:
            raise FormatError(f"items 第 {i} 项：field 写的是「{f}」，只能是 {'／'.join(FC.FIELDS)} 之一")
        t = it.get("text")
        if not isinstance(t, str) or not t.strip():
            raise FormatError(f"items 第 {i} 项：缺少 text（题面原文片段）")
        out.append({"field": f, "text": t.strip(), "zh": _s(it.get("zh"))})
    terms = []
    for i, t in enumerate(obj.get("terms") or [], 1):
        if not isinstance(t, dict) or not _s(t.get("term")):
            raise FormatError(f"terms 第 {i} 项缺少 term（原词）")
        rg = _s(t.get("range"))
        if rg not in FC.RANGES:
            raise FormatError(f"terms 第 {i} 项：range 只能是 相等／更窄／更宽")
        if rg != "相等" and len(_s(t.get("lost"))) < 2:
            raise FormatError(f"terms 第 {i} 项：range 是 {rg}，lost 必须写明丢掉了什么")
        for k in ("definition", "compute"):
            if not _s(t.get(k)):
                raise FormatError(f"terms 第 {i} 项缺少 {k}")
        terms.append({"term": _s(t["term"]), "definition": _s(t["definition"]), "compute": _s(t["compute"]),
                      "range": rg, "lost": _s(t.get("lost"))})
    imp = []
    for i, h in enumerate(obj.get("implicit") or [], 1):
        if not isinstance(h, dict) or not _s(h.get("content")):
            raise FormatError(f"implicit 第 {i} 项缺少 content")
        f = _s(h.get("field"))
        if f not in FC.FIELDS:
            raise FormatError(f"implicit 第 {i} 项：field 只能是 {'／'.join(FC.FIELDS)} 之一")
        if len(_s(h.get("reason"))) < 2:
            raise FormatError(f"implicit 第 {i} 项：必须写 reason（为什么非补不可）")
        imp.append({"field": f, "content": _s(h["content"]), "reason": _s(h["reason"])})
    return {"items": out, "terms": terms, "implicit": imp}


def validate_form(problem, expected_opinions=None):
    """表单的校验：格式＋程序逐字核对（不通过就把具体哪一处对不上告诉它，按格式重试处理）。"""
    def v(obj):
        form = _clean_form(obj)
        chk = FC.check_form(problem, form)
        if not chk["ok"]:
            raise FormatError("程序逐字核对没有通过：" + "；".join(chk["errors"][:8]))
        if expected_opinions is not None:
            rs = obj.get("responses")
            if not isinstance(rs, list):
                raise FormatError("修改轮必须有 responses 列表，每条意见一项")
            seen = {}
            for i, r in enumerate(rs, 1):
                if not isinstance(r, dict) or r.get("decision") not in ("采纳", "驳回"):
                    raise FormatError(f"responses 第 {i} 项：decision 只能是 采纳 或 驳回")
                if len(_s(r.get("check"))) < 4:
                    raise FormatError(f"responses 第 {i} 项：check 要写明你怎么核实的")
                seen[str(r.get("opinion")).strip()] = {"opinion": str(r.get("opinion")).strip(),
                                                       "decision": r["decision"], "check": _s(r["check"])}
            miss = [o for o in expected_opinions if o not in seen]
            if miss:
                raise FormatError("这些意见没有回应：" + "、".join(miss))
            form["responses"] = [seen[o] for o in expected_opinions]
        else:
            form["responses"] = []
        form["check"] = {"hints": chk["hints"], "added": chk["added"]}
        obj.clear()
        obj.update(form)
    return v


SYSTEM_FORM_REVIEWER = """你是多模型互查流程中的【审查者】，这一步审的是几位生产者各自把题面拆成的表单（用甲、乙、丙标注）。你不做题。
之后做题的人只看冻结的表单、看不到原题，所以表单必须和题面一一对应：A 对 A、B 对 B、非 C 对非 C、求 D 对 D。
逐份对照题面原文检查：
- 遗漏：题面里有、表单里没有（尤其是量词、范围、否定词，如 "over all pairs"、"for sufficiently large k"）；
- 多余：表单里有、题面里没有（悄悄加了条件、缩小或扩大了范围、把题目改成了熟悉的标准形式）；
- 改了意思、归类错误、切分不当（一项里塞了好几个条件）；
- 术语展开是否完整、范围比较是否如实（例如把 k 阶缩成 1 阶却写"相等"）；
- 隐含项是否真的非补不可、补得对不对。
程序已经做过逐字核对（每项都能在原文里找到、原文没有漏项），并把各份表单在归类上的差异列给你；【】里的补词是模型自己加的，要特别看。
意见分两类：硬伤＝你能指出原文对应的那一段（quote 逐字引自题面或该表单）并说明哪里不符，basis 写：遗漏／多余／改了意思／归类错误／切分不当／术语展开不当／隐含项不当；次要＝不影响意思的写法问题。
每份表单的总体评价：通过＝没有硬伤；有问题＝至少一条该对象的硬伤意见。没发现问题就写通过，不要为了凑数挑毛病。
problem 是写给该生产者本人看的，不要提到其他生产者、不要写出他们的表单内容。
terms_consistent：各份表单的术语展开是否本质一致（是／否）；implicit_consistent：各份表单的隐含项是否本质一致（都没有也算一致）（是／否）。
只输出一个 JSON 对象。"""


def schema_form_review(labels):
    ov = ", ".join(f'"{l}": "通过 / 有问题"' for l in labels)
    return ('{"overall": {' + ov + '}, "opinions": [{"target": "' + labels[0] + '", "severity": "硬伤 / 次要", '
            '"basis": "（硬伤必填）遗漏 / 多余 / 改了意思 / 归类错误 / 切分不当 / 术语展开不当 / 隐含项不当", '
            '"quote": "（硬伤必填）题面或该表单里对应的那一段，逐字", "problem": "写给该生产者的问题"}], '
            '"terms_consistent": "是 / 否", "implicit_consistent": "是 / 否"}\n'
            '说明：opinions 没有就写 []；target 只能是：' + "、".join(labels) + '。')


def form_review_prompt(problem, forms, diffs, lang=None):
    p = [f"题面原文：\n{problem}\n{lang_block(lang)}", "\n各生产者的表单："]
    for lab, f in forms.items():
        p.append(form_text(f, lab))
        add = (f.get("check") or {}).get("added") or []
        if add:
            p.append("  （程序列出：这份表单的补词＝" + "、".join(f"第{a['项']}项【{a['补词']}】" for a in add) + "，含模型判断）")
        hints = (f.get("check") or {}).get("hints") or []
        for h in hints:
            p.append(f"  （程序提示：{h}）")
    if diffs:
        p.append("\n程序发现的归类差异（同一段原文，各份表单归到了不同的栏目）：")
        for d in diffs:
            p.append(f"- 「{d['原文']}」：" + "；".join(f"{k}→{v}" for k, v in d["各方归类"].items()))
    else:
        p.append("\n程序没有发现归类差异。")
    labels = list(forms)
    p.append(f"\n按下面的格式输出 JSON：\n{schema_form_review(labels)}")
    return "\n".join(p)


def validate_form_review(labels, problem, forms):
    def v(obj):
        if not isinstance(obj, dict) or not isinstance(obj.get("overall"), dict):
            raise FormatError("缺少 overall（对每份表单的总体评价）")
        for l in labels:
            if obj["overall"].get(l) not in ("通过", "有问题"):
                raise FormatError(f"overall.{l} 只能是 通过 或 有问题")
        ops = obj.get("opinions") or []
        if not isinstance(ops, list):
            raise FormatError("opinions 必须是列表")
        clean = []
        for i, o in enumerate(ops, 1):
            if not isinstance(o, dict) or o.get("target") not in labels or not _s(o.get("problem")):
                raise FormatError(f"opinions 第 {i} 项：要有 target（{'、'.join(labels)}）和 problem")
            sev = "次要" if _s(o.get("severity")).startswith("次") else "硬伤"
            basis = ""
            if sev == "硬伤":
                hit = [b for b in FORM_BASES if b in _s(o.get("basis"))]
                basis = hit[0] if len(hit) == 1 else ""
                if not basis:
                    raise FormatError(f"opinions 第 {i} 项：硬伤必须写 basis（{'／'.join(FORM_BASES)} 之一）")
                q = _s(o.get("quote"))
                if not (quote_in(q, problem) or quote_in(q, form_text(forms[o["target"]]))):
                    raise FormatError(f"opinions 第 {i} 项：硬伤的 quote 必须逐字引自题面原文或该表单（现在是「{q}」，找不到）")
            clean.append({"target": o["target"], "severity": sev, "basis": basis, "quote": _s(o.get("quote")),
                          "problem": _s(o["problem"]), "layer": "表单", "location": _s(o.get("quote"))[:60] or "（未指明）"})
        for l in labels:
            if obj["overall"][l] == "有问题" and not any(c["target"] == l and c["severity"] == "硬伤" for c in clean):
                raise FormatError(f"{l} 被评为有问题，但没有一条针对它的硬伤意见")
        obj["opinions"] = clean
        obj["terms_consistent"] = "是" if _s(obj.get("terms_consistent")) == "是" else "否"
        obj["implicit_consistent"] = "是" if _s(obj.get("implicit_consistent")) == "是" else "否"
    return v


# ================================================================ 找茬者与复查（两段共用）

SYSTEM_FAULTFINDER = """你是多模型互查流程中的【找茬者】。你不做题，也不验证谁对谁错，只做一件事：
找出几位生产者（甲、乙、丙）是否【共用了同一个没有说出来的前提或方法】、他们的材料之间有没有【冲突】（一方引用的定理被另一方举的例子推翻、一方前后矛盾），必要时提出一条【替代方法】的方向。
规则：
1. 【找到即止】找到值得一查的问题就写下来，不要去证明、不要去求最终答案，也不要为了凑数列很多条。
2. 每条问题都要有 quotes：逐字引自对应生产者材料的原文（程序会核对引文确实存在），并写明 targets（涉及哪几位）。
3. 你的意见只交给审查者核实，不直接发给生产者，也不挡闭合；所以写清楚就行，不用下结论。
只输出一个 JSON 对象。"""


def schema_faultfinder(labels):
    return ('{"issues": [{"type": "共同前提 / 冲突 / 替代方法 / 其他", "targets": ["' + labels[0] + '"], '
            '"quotes": [{"label": "' + labels[0] + '", "text": "逐字引自该生产者的原文"}], "content": "问题是什么"}]}\n'
            '说明：没有发现就写 {"issues": []}；label、targets 只能是：' + "、".join(labels) + '。')


def faultfinder_prompt(header, materials, lang=None):
    """header：题面（表单阶段）或冻结的表单（做题阶段）；materials：{标签: 文字}。"""
    p = [header, lang_block(lang), "\n各生产者的材料："]
    for lab, txt in materials.items():
        p.append(f"\n【生产者{lab}】\n{txt}")
    p.append(f"\n按下面的格式输出 JSON：\n{schema_faultfinder(list(materials))}")
    return "\n".join(p)


def validate_faultfinder(materials):
    labels = list(materials)

    def v(obj):
        if not isinstance(obj, dict) or not isinstance(obj.get("issues"), list):
            raise FormatError("缺少 issues 列表（没有发现就写 []）")
        clean = []
        for i, x in enumerate(obj["issues"], 1):
            if not isinstance(x, dict) or not _s(x.get("content")):
                raise FormatError(f"issues 第 {i} 项缺少 content")
            tg = [t for t in (x.get("targets") or []) if t in labels]
            if not tg:
                raise FormatError(f"issues 第 {i} 项：targets 只能写 {'、'.join(labels)}")
            qs = []
            for q in x.get("quotes") or []:
                if not isinstance(q, dict) or q.get("label") not in labels:
                    continue
                if not quote_in(q.get("text"), materials[q["label"]]):
                    raise FormatError(f"issues 第 {i} 项：引文「{_s(q.get('text'))[:60]}」在生产者{q['label']}的材料里找不到（必须逐字引用）")
                qs.append({"label": q["label"], "text": _s(q["text"])})
            if not qs:
                raise FormatError(f"issues 第 {i} 项：至少要有一条逐字引文（quotes）")
            clean.append({"type": _s(x.get("type")) or "其他", "targets": tg, "quotes": qs, "content": _s(x["content"])})
        obj["issues"] = clean
    return v


SYSTEM_RECHECK = """你是【审查者】，在复查找茬者找出的问题。你已经独立审过一遍；现在逐条核实找茬者的问题：
- 采纳＝你核实后认为它成立：写出一条发给相关生产者的意见（target、severity、problem）。severity 写硬伤时必须是被反驳了（basis：反例／自相矛盾／计算错误／与口径冲突／遗漏／多余／改了意思 等），没被反驳的只能写次要或不采纳。
- 驳回＝不成立或不影响结论，写明理由。
找茬者不知道谁对谁错，你也不要因为它提了就照单全收。problem 是写给该生产者本人看的，不要提到其他生产者、不要写出他们的答案。
只输出一个 JSON 对象。"""


def schema_recheck(labels, n):
    one = ('{"issue": "1", "decision": "采纳 / 驳回", "reason": "核实经过", '
           '"opinion": {"target": "' + labels[0] + '", "severity": "硬伤 / 次要", "basis": "（硬伤必填）", "problem": "…"}}')
    return ('{"decisions": [' + one + ']}\n说明：issues 共 ' + str(n) + ' 条，每条一项；驳回时 opinion 可省略；target 只能是：'
            + "、".join(labels) + '。')


def recheck_prompt(header, issues, context, labels, lang=None):
    p = [header, lang_block(lang), "\n你之前审查时的材料摘要：", context, "\n找茬者找出的问题："]
    for i, x in enumerate(issues, 1):
        q = "；".join(f"{y['label']}：「{y['text']}」" for y in x["quotes"])
        p.append(f"{i}. 〔{x['type']}｜涉及 {'、'.join(x['targets'])}〕{x['content']}\n   引文：{q}")
    p.append(f"\n按下面的格式输出 JSON：\n{schema_recheck(labels, len(issues))}")
    return "\n".join(p)


def validate_recheck(labels, n, bases):
    def v(obj):
        ds = obj.get("decisions") if isinstance(obj, dict) else None
        if not isinstance(ds, list):
            raise FormatError("缺少 decisions 列表")
        got = {}
        for i, d in enumerate(ds, 1):
            if not isinstance(d, dict) or d.get("decision") not in ("采纳", "驳回"):
                raise FormatError(f"decisions 第 {i} 项：decision 只能是 采纳 或 驳回")
            k = str(d.get("issue", i)).strip()
            op = None
            if d["decision"] == "采纳":
                o = d.get("opinion") or {}
                if o.get("target") not in labels or not _s(o.get("problem")):
                    raise FormatError(f"decisions 第 {i} 项：采纳时 opinion 要有 target（{'、'.join(labels)}）和 problem")
                sev = "次要" if _s(o.get("severity")).startswith("次") else "硬伤"
                basis = ""
                if sev == "硬伤":
                    hit = [b for b in bases if b in _s(o.get("basis"))]
                    basis = hit[0] if len(hit) == 1 else ""
                    if not basis:
                        raise FormatError(f"decisions 第 {i} 项：硬伤必须写 basis（{'／'.join(bases)} 之一）；没被反驳的请写次要")
                # 来源只记在账本里；发给生产者的意见不注明来自找茬者（用户 2026-10-03 定：选"不注明"）
                op = {"target": o["target"], "severity": sev, "basis": basis, "problem": _s(o["problem"]),
                      "layer": "", "location": "（未指明）", "internal_note": ""}
            got[k] = {"issue": k, "decision": d["decision"], "reason": _s(d.get("reason")), "opinion": op}
        miss = [str(i) for i in range(1, n + 1) if str(i) not in got]
        if miss:
            raise FormatError("这些问题没有给结论：" + "、".join(miss))
        obj["decisions"] = [got[str(i)] for i in range(1, n + 1)]
    return v


# ================================================================ 第二段：逐份审

SYSTEM_REVIEW_ONE = """你是多模型互查流程中的【审查者】，像一位批改作业的老师：你不做题。这一次你只审【一份】答卷（用它的代号标注），其他答卷另有单独的审查。
题目以【冻结的表单】为准（做题的人也只看到这张表单），表单里每一条要求都有编号。
你要做的：
1. 逐条审查（requirements）：对表单里每一条要求（排除项除外）给结论：满足／不满足／无法判断。不满足必须有反驳（在 opinions 里写一条硬伤，requirement 填这一条的编号）；无法判断＝没被反驳、但也没被证明或验证到，写进 unverified_parts，不挡闭合。
2. 额外前提（extra_premises）：答卷用到了、但表单里没有的前提，逐字引出原文（quote）；与表单某条冲突的（例如表单说"over all pairs"，答卷却默认固定了底集），conflict_req 写那一条的编号，并把那一条判为不满足。
3. 【严格度只有一把尺子：表单】只按表单里的完成标准判断，排除项列的事不要求它做（例如排除项写了"不要求证明"，就不要因为没证明而判硬伤）。basis 写"与口径冲突"的意见必须在 requirement 写明是哪一条。
4. 【反驳够用即止】你的目的是对这份答卷给出判决（对／不对＋依据），不是自己求出答案。找到一个成立的反驳（反例、更大的构造、自相矛盾、算错的一步）就停下写进意见，不要继续去找最优构造、做渐近分析或自己解题。own_answer 一栏留空；只有在你确实顺手得到了、并且想如实记录时才填（填了会在报告里标出"审查者越界"）。
5. 意见分两类：硬伤＝被反驳了，basis 只能写：反例／自相矛盾／计算错误／与口径冲突／弃权理由不成立，并在 problem 里写出反驳的具体内容；未验证的部分＝没被反驳但没证明或没验证到，写进 unverified_parts（what、tried 必填）。没有证明不等于错误。
6. 摘要（summary）：写给你自己之后对照用的。answer＝它的答案；claims＝它的关键论断；theorems＝它引用的定理或结果（name＋quote）；examples＝它给出的具体数值和例子；route＝一句话概括它的路线。claims、theorems.quote、examples 每一条都必须【逐字引自答卷原文】（程序会核对，只能少写，不能改写或编造）。上界、最优性这类结论所依赖的定理一定要列进去。
7. 独立验证（implementation，可选）：只依据它【文字写的 verify_plan】写一份你自己的验证代码（不是替它解题），必填 check_type：同路线复现／另一方法／未能换方法；能换方法就尽量换。
8. 总体评价 overall：通过＝没有被反驳；有问题＝至少一条硬伤；不确定＝材料残缺读不懂。problem 只谈这份答卷，不要提到其他答卷。
只输出一个 JSON 对象。"""

# 2.8：表单阶段关闭时（默认）做题阶段用原题。只有一条要审的"要求"，编号固定为"原题"。
# 提示词按用户的提示词原则写：只写动作和判定规则，不用角色比喻、评价词。
RAW_REQ = "原题"
SYSTEM_REVIEW_ONE_RAW = """你是多模型互查流程中的【审查者】，这一步不做题。这一次只审【一份】答卷（用它的代号标注），其他答卷另有单独的审查。
题目以【原题】和【口径】为准（做题的人看到的也是原题和口径）。
要做的：
1. 原题审查（requirements）：只有一条，编号"原题"：答卷是否按原题和口径作答、答案是否成立。结论：满足／不满足／无法判断。不满足必须有反驳（在 opinions 里写一条硬伤，requirement 填"原题"）；无法判断＝没被反驳、也没被证明或验证到，写进 unverified_parts，不挡闭合。
2. 额外前提（extra_premises）：答卷用到了、原题和口径里都没有的前提，逐字引出答卷原文（quote）；与原题或口径冲突的，conflict_req 写"原题"，把"原题"判为不满足，并在硬伤意见的 problem 里逐字引出原题或口径里与它冲突的那句。
3. 严格度只按口径判断：口径没要求的事，不作为硬伤。basis 写"与口径冲突"的意见，requirement 填"原题"。
4. 判决依据找到一个即可：找到一个成立的反驳（反例、更大的构造、自相矛盾、算错的一步）就写进意见并停止，不继续求最优构造、做渐近分析或解题。own_answer 一栏留空；确实得到了、需要记录时才填（填了会在报告里标出"审查者越界"）。
5. 意见分两类：硬伤＝被反驳了，basis 只能写：反例／自相矛盾／计算错误／与口径冲突／弃权理由不成立，并在 problem 里写出反驳的具体内容；未验证的部分＝没被反驳但没证明或没验证到，写进 unverified_parts（what、tried 必填）。没有证明不等于错误。
6. 摘要（summary）：供你之后对照用。answer＝它的答案；claims＝它的关键论断；theorems＝它引用的定理或结果（name＋quote）；examples＝它给出的具体数值和例子；route＝一句话概括它的路线。claims、theorems.quote、examples 每一条都必须逐字引自答卷原文（程序会核对，只能少写，不能改写或编造）。结论所依赖的定理都列进去。
7. 独立验证（implementation，可选）：只依据它文字写的 verify_plan 写一份验证代码（不替它解题），必填 check_type：同路线复现／另一方法／未能换方法；能换方法就换。
8. 总体评价 overall：通过＝没有被反驳；有问题＝至少一条硬伤；不确定＝材料残缺读不懂。problem 只谈这份答卷，不提其他答卷。
只输出一个 JSON 对象。"""


def schema_review_one(label, req_ids, raw=False):
    rq = ", ".join(f'{{"req": "{r}", "verdict": "满足 / 不满足 / 无法判断", "note": "…"}}' for r in req_ids)
    prem = ('"extra_premises": [{"quote": "答卷原文", "conflict_req": "（与原题或口径冲突时写 原题，否则留空）"}], ' if raw else
            '"extra_premises": [{"quote": "答卷原文", "conflict_req": "（与表单某条冲突时写条号，否则留空）"}], ')
    note = (f'说明：这份答卷的代号是 {label}；requirements 只有"原题"一条，照填即可；没有的列表写 []；不写实现时 implementation 写 null。'
            if raw else
            f'说明：这份答卷的代号是 {label}；requirements 已按表单列好（排除项不用列），照填即可；没有的列表写 []；不写实现时 implementation 写 null。')
    return ('{"overall": "通过 / 有问题 / 不确定", "requirements": [' + rq + '], '
            '"opinions": [{"severity": "硬伤 / 次要", "basis": "（硬伤必填）反例 / 自相矛盾 / 计算错误 / 与口径冲突 / 弃权理由不成立", '
            '"requirement": "（basis 为与口径冲突或该条判为不满足时必填：条号）", "problem": "…", "layer": "（选填）", "location": "（选填）"}], '
            + prem +
            '"unverified_parts": [{"what": "…", "tried": "…"}], '
            '"implementation": {"check_type": "同路线复现 / 另一方法 / 未能换方法", "check_note": "…", "explain": "…", "code": "…"}, '
            '"summary": {"answer": "…", "claims": ["逐字引文"], "theorems": [{"name": "…", "quote": "逐字引文"}], '
            '"examples": ["逐字引文"], "route": "一句话"}, "own_answer": ""}\n' + note)


def paper_text(view):
    """一份答卷的全文（给逐份审、找茬者看；也是摘要引文核对的依据）。"""
    s = sub_block(view)
    s += f"\n验证方案：{json.dumps(view.get('verify_plan') or {}, ensure_ascii=False)}\n它声明的局限：{view.get('limits') or '（无）'}"
    if view.get("responses"):
        s += "\n它对上一轮意见的回应：" + "".join(f"\n  - 意见「{r.get('opinion_text', r.get('opinion'))}」→ {r['decision']}；核实：{r['check']}"
                                          for r in view["responses"])
    if view.get("changes"):
        s += "\n它声明的改动：" + "；".join(c["what"] for c in view["changes"])
    return s


def review_one_prompt(form_txt, scope, view, req_ids, libs, allow_code, lang=None, raw=False):
    p = [("【原题】\n" + form_txt) if raw else form_txt, scope_block(scope), lang_block(lang), f"\n【答卷{view['label']}】\n{paper_text(view)}"]
    if view.get("author_ran") is not None and view.get("status", "得出") != "未得出":
        p.append(f"（程序记录：它的验证代码{'已运行' if view['author_ran'] else '没有运行'}；结果稍后单独给你，写你自己的实现时先不要看。）")
    p.append(f"\n{libs_line(libs, allow_code)}（这句话也适用于你写的实现。）\n\n按下面的格式输出 JSON：\n{schema_review_one(view['label'], req_ids, raw)}")
    return "\n".join(p)


def validate_review_one(label, req_ids, excluded, paper, src="表单"):
    from .final_answer import CHECK_TYPES, OVERALL

    def v(obj):
        if not isinstance(obj, dict):
            raise FormatError("返回的不是 JSON 对象")
        ov = _s(obj.get("overall"))
        if ov not in OVERALL:
            raise FormatError("overall 只能是 通过 / 有问题 / 不确定")
        got = {}
        for i, r in enumerate(obj.get("requirements") or [], 1):
            if not isinstance(r, dict):
                continue
            k = str(r.get("req", "")).strip()
            vd = _s(r.get("verdict"))
            if vd not in ("满足", "不满足", "无法判断"):
                raise FormatError(f"requirements 第 {i} 项：verdict 只能是 满足／不满足／无法判断")
            got[k] = {"req": k, "verdict": vd, "note": _s(r.get("note"))}
        miss = [k for k in req_ids if k not in got]
        if miss:
            raise FormatError("这些要求没有给结论：第" + "、".join(miss) + "条（逐条审查，每条一个结论）")
        ops, out_of_scope = [], []
        for i, o in enumerate(obj.get("opinions") or [], 1):
            if not isinstance(o, dict) or not _s(o.get("problem")):
                raise FormatError(f"opinions 第 {i} 项缺少 problem")
            sev = "次要" if _s(o.get("severity")).startswith("次") else "硬伤"
            basis = _norm_basis(o.get("basis")) if sev == "硬伤" else ""
            req = str(o.get("requirement") or "").strip()
            if sev == "硬伤" and not basis:
                raise FormatError(f"opinions 第 {i} 项：硬伤必须写 basis（{'／'.join(BASES[:5])} 之一）；没被反驳的请写进 unverified_parts")
            if req and req not in req_ids and req not in excluded:
                raise FormatError(f"opinions 第 {i} 项：requirement 写的是「{req}」，{src}里没有这一条" + ("（只能写 原题）" if src == "原题" else ""))
            if sev == "硬伤" and basis == "与口径冲突":
                if not req:
                    raise FormatError(f"opinions 第 {i} 项：basis 是与口径冲突，必须在 requirement 写明" + ("「原题」" if src == "原题" else "是表单哪一条"))
                if req in excluded:  # 2.4 过严的回归：拿排除项当硬伤——程序判为超出口径，不挡闭合
                    sev, basis = "次要", ""
                    out_of_scope.append(req)
            ops.append({"target": label, "severity": sev, "basis": basis, "requirement": req,
                        "layer": _s(o.get("layer")), "location": _s(o.get("location")) or "（未指明）",
                        "problem": _s(o["problem"]) + ("（程序注：所引的是排除项，超出口径，按次要处理、不挡闭合）" if req in out_of_scope else ""),
                        "internal_note": ""})
        for k, r in got.items():
            if r["verdict"] == "不满足" and not any(o["requirement"] == k and o["severity"] == "硬伤" for o in ops):
                raise FormatError(f"第{k}条判为不满足，但没有一条 requirement 为 {k} 的硬伤意见（不满足必须附反驳）")
        if ov == "有问题" and not any(o["severity"] == "硬伤" for o in ops):
            raise FormatError("overall 是有问题，但没有硬伤意见；没被反驳就评为通过，把没验证到的写进 unverified_parts")
        prem = []
        for i, e in enumerate(obj.get("extra_premises") or [], 1):
            if not isinstance(e, dict) or not _s(e.get("quote")):
                continue
            if not quote_in(e["quote"], paper):
                raise FormatError(f"extra_premises 第 {i} 项：quote 必须逐字引自这份答卷（「{_s(e['quote'])[:60]}」找不到）")
            cr = str(e.get("conflict_req") or "").strip()
            if cr and cr not in req_ids:
                raise FormatError(f"extra_premises 第 {i} 项：conflict_req 写的「{cr}」" + ("只能写 原题" if src == "原题" else "不是表单里需要审查的条号"))
            if cr and got[cr]["verdict"] != "不满足":
                raise FormatError(f"extra_premises 第 {i} 项与第{cr}条冲突，那一条就应判为不满足（并附硬伤意见）")
            prem.append({"quote": _s(e["quote"]), "conflict_req": cr})
        up = []
        for i, u in enumerate(obj.get("unverified_parts") or [], 1):
            if not isinstance(u, dict) or not _s(u.get("what")):
                raise FormatError(f"unverified_parts 第 {i} 项缺少 what")
            if len(_s(u.get("tried"))) < 2:
                raise FormatError(f"unverified_parts 第 {i} 项缺少 tried（没试就写“没有试”）")
            up.append({"target": label, "what": _s(u["what"]), "tried": _s(u["tried"])})
        im = obj.get("implementation")
        impls = []
        if isinstance(im, dict) and isinstance(im.get("code"), str) and im["code"].strip():
            ct = _s(im.get("check_type"))
            hit = [c for c in CHECK_TYPES if c in ct]
            ct = hit[0] if len(hit) == 1 else ""
            if not ct:
                raise FormatError(f"implementation：缺少 check_type（{'／'.join(CHECK_TYPES)}）")
            note = _s(im.get("check_note"))
            if ct != "同路线复现" and len(note) < 4:
                raise FormatError("implementation：check_type 不是同路线复现时，check_note 必须写明方法或原因")
            impls.append({"target": label, "explain": _s(im.get("explain")), "code": im["code"], "check_type": ct,
                          "check_note": note})
        own = _s(obj.get("own_answer"))
        sm = obj.get("summary") if isinstance(obj.get("summary"), dict) else {}
        summary = {"answer": _s(sm.get("answer")), "route": _s(sm.get("route")), "claims": [], "theorems": [], "examples": []}
        for key in ("claims", "examples"):
            for i, q in enumerate(sm.get(key) or [], 1):
                q = q if isinstance(q, str) else _s((q or {}).get("quote") if isinstance(q, dict) else "")
                if not q.strip():
                    continue
                if not quote_in(q, paper):
                    raise FormatError(f"summary.{key} 第 {i} 项必须逐字引自答卷原文（「{q[:60]}」找不到）；只能少写，不能改写")
                summary[key].append(q.strip())
        for i, t in enumerate(sm.get("theorems") or [], 1):
            if not isinstance(t, dict) or not _s(t.get("quote")):
                continue
            if not quote_in(t["quote"], paper):
                raise FormatError(f"summary.theorems 第 {i} 项的 quote 必须逐字引自答卷原文（「{_s(t['quote'])[:60]}」找不到）")
            summary["theorems"].append({"name": _s(t.get("name")), "quote": _s(t["quote"])})
        obj.clear()
        obj.update({"overall": ov, "requirements": [got[k] for k in req_ids], "opinions": ops, "extra_premises": prem,
                    "unverified_parts": up, "implementations": impls, "summary": summary,
                    "own_answer": own, "out_of_scope": out_of_scope})
    return v


def summary_text(label, status, answer, sm):
    p = [f"【答卷{label}】状态：{status}；答案：{answer or '（无）'}；路线：{sm.get('route') or '（未写）'}"]
    for q in sm.get("claims") or []:
        p.append(f"  论断：「{q}」")
    for t in sm.get("theorems") or []:
        p.append(f"  引用：{t.get('name') or '（未命名）'}——「{t['quote']}」")
    for q in sm.get("examples") or []:
        p.append(f"  例子/数值：「{q}」")
    return "\n".join(p)


# ================================================================ 第二段：对照（每个审查者把自己几份摘要放在一起比）

SYSTEM_COMPARE = """你是【审查者】，刚才分别审完了几份答卷，并给每份写了摘要（引文都逐字来自答卷原文）。现在把这几份摘要放在一起对照，只做三件事：
1. 材料间冲突检查（必填）：逐条列出摘要之间相互冲突的地方——一方引用的定理或结论被另一方举的例子或数值推翻、两方对同一个量给出不同的值、一方前后自相矛盾。每条写出冲突的两处（逐字引自摘要里的引文），说明为什么冲突，并写明谁需要回应（refuted：代号，或"都要回应"）。确实被反驳的（例如例子推翻了引用的上界）severity 写硬伤；只是可疑、需要对方核实的写提示。没有冲突就写 []。
2. same_route：各份答卷走的路线是否本质相同（true／false）。
3. answers_equivalent：各答案是否等价（是／否／不确定；程序拿不准时才需要认真填）。
不要重新审每份答卷，也不要自己解题。只输出一个 JSON 对象。"""


def schema_compare(labels):
    return ('{"conflicts": [{"a": "' + labels[0] + '", "quote_a": "逐字引自 a 的摘要", "b": "' + labels[-1] + '", '
            '"quote_b": "逐字引自 b 的摘要", "explain": "为什么冲突", "severity": "硬伤 / 提示", "refuted": "代号 或 都要回应"}], '
            '"same_route": true 或 false, "answers_equivalent": "是 / 否 / 不确定"}\n'
            '说明：a、b 只能是：' + "、".join(labels) + '（同一份答卷前后矛盾时 a、b 写同一个代号）。')


def compare_prompt(header, summaries, facts, lang=None):
    p = [header, lang_block(lang), "\n你写的各份摘要：", "\n".join(summaries.values()),
         f"\n程序对各答案的基础比对（只做了格式规范化和数值验证）：{facts}",
         f"\n按下面的格式输出 JSON：\n{schema_compare(list(summaries))}"]
    return "\n".join(p)


def validate_compare(summaries):
    labels = list(summaries)

    def v(obj):
        if not isinstance(obj, dict):
            raise FormatError("返回的不是 JSON 对象")
        cs = obj.get("conflicts")
        if not isinstance(cs, list):
            raise FormatError("conflicts 必须是列表（材料间冲突检查是必填项，没有冲突就写 []）")
        clean = []
        for i, c in enumerate(cs, 1):
            if not isinstance(c, dict) or c.get("a") not in labels or c.get("b") not in labels:
                raise FormatError(f"conflicts 第 {i} 项：a、b 只能是 {'、'.join(labels)}")
            for side in ("a", "b"):
                q = _s(c.get("quote_" + side))
                if not quote_in(q, summaries[c[side]]):
                    raise FormatError(f"conflicts 第 {i} 项：quote_{side}「{q[:60]}」不在答卷{c[side]}的摘要里（必须逐字引用）")
            ref = _s(c.get("refuted"))
            tg = [ref] if ref in labels else sorted({c["a"], c["b"]}, key=labels.index)
            sev = "硬伤" if _s(c.get("severity")).startswith("硬") else "提示"
            clean.append({"a": c["a"], "quote_a": _s(c["quote_a"]), "b": c["b"], "quote_b": _s(c["quote_b"]),
                          "explain": _s(c.get("explain")), "severity": sev, "targets": tg})
        obj["conflicts"] = clean
        obj["same_route"] = obj.get("same_route") is True
        if obj.get("answers_equivalent") not in ("是", "否", "不确定"):
            obj["answers_equivalent"] = "不确定"
    return v


# ================================================================ 裸题对照

SYSTEM_BARE = "你是一个有帮助的助手。"


def bare_prompt(problem):
    """2.7 裸题对照：只给题目本身。唯一的附加是"最后一行写最终答案"，否则程序没法把答案取出来比对。"""
    return f"{problem}\n\n请在回答的最后一行写：最终答案：……"


_FINAL = re.compile(r"最终答案\s*[:：]\s*(.+)")


def bare_answer(text):
    """从裸题回答里取最终答案：最后一个"最终答案："那一行；没有就取最后一个 \\boxed{…}。都没有返回空。"""
    hits = _FINAL.findall(text or "")
    if hits:
        a = hits[-1].strip()
    else:
        bx = re.findall(r"\\boxed\{(.+)\}", text or "")
        a = bx[-1].strip() if bx else ""
    a = a.strip().strip("$").strip()
    m = re.fullmatch(r"\\boxed\{(.*)\}", a)
    return (m.group(1) if m else a).strip()
