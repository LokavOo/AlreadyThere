"""题目包：数学最终答案题（2.2 评审流程；2.4 起答案分三态）。

每个条目是一道题：{"id", "name"(短标签，可省), "problem"(题面), "gold"(标准答案，只在最后由程序比对，永不进入任何提示词)}。
流程见 flow_review.py：只有生产者做题；审查者不做题，参考"表示层、方法层、约束层、验证层"这几个角度比对各生产者的提交，
并按生产者的文字描述独立写一份验证代码来运行；意见发回生产者（互相隔离）；生产者先核实再采纳或驳回；
全部意见都关闭且真的一致才算闭合，否则交给用户裁决。
2.4：生产者可以如实写"未得出"或"部分得出"（并说明试过什么、卡在哪、缺什么），"不知道"是合法答案，不会被格式校验打回。
"""

from __future__ import annotations

import json
import re

from ..fields.answer import AnswerField, compare, normalize  # noqa: F401
from ..store import Project  # noqa: F401
from ..util import FormatError

FIELD = AnswerField()
LAYERS = ("表示层", "方法层", "约束层", "验证层")  # 2.4：只是提示词里给模型的参考角度，不再用于校验
LABELS = "甲乙丙丁戊己庚辛"
SEVERITY = ("硬伤", "次要")
# 2.5 意见严重度：硬伤＝被反驳了（必须写明依据）；没被反驳但没证明/没验证到的不是硬伤，写进"未验证的部分"
BASES = ("反例", "自相矛盾", "计算错误", "与口径冲突", "弃权理由不成立", "验证结果不一致")
CHECK_TYPES = ("同路线复现", "另一方法", "未能换方法")
LANGS = ("中文", "English", "日本語", "한국어", "Français", "Deutsch", "Español", "Русский")
OVERALL = ("通过", "有问题", "不确定")
STATUSES = ("得出", "部分得出", "未得出")


# ---------------------------------------------------------------- 提示词

def libs_line(libs, allow_code):
    if not allow_code:
        return "本次不运行任何代码：code 字段留空字符串，verify_plan 仍要写，说明如果能运行代码你会怎么验证。"
    have = [f"{k} {v}" for k, v in (libs or {}).items() if v]
    miss = [k for k, v in (libs or {}).items() if not v]
    s = "代码是 Python，由程序在本机的子进程里运行，最长约 60 秒，不能联网，不要读写文件，把结果 print 出来（不超过 30 行）。"
    s += f"可以用标准库和这些已装的库：{('、'.join(have) if have else '（没有第三方库）')}。"
    if miss:
        s += f"没装的库不要用：{'、'.join(miss)}。"
    s += "只用你确定存在的函数；拿不准某个库有没有某个函数时，改用标准库或自己写几行实现，不要凭印象调用。"
    return s


SCOPE_IN_PROBLEM = "（口径写在题面里）"  # 2.9：主窗口输入框建的题，口径就是题面的一部分，不另设


def scope_block(scope):
    """2.5：口径是严格度的唯一来源。题目提出者写的口径原样交给每个角色，并说明怎么用。"""
    sc = (scope or "").strip()
    if sc == SCOPE_IN_PROBLEM:
        return ("\n【口径】口径写在题面里：题面里关于“什么算做完”的要求就是这道题严格度的唯一尺度。"
                "只按题面判断，不提高也不放宽要求；题面之外你认为值得知道的事，如实写进“限制／未验证的部分”，但不要因此改变状态或评价。")
    if not sc:
        return ("\n【口径】题目没有写口径：按题目字面要求判断，不要自行提高或放宽要求；"
                "你认为值得知道的其他事，如实写进“限制／未验证的部分”，但不要因此改变状态或评价。")
    return ("\n【口径】（题目提出者规定的“什么算做完”，也是这道题严格度的唯一尺度）：" + sc +
            "\n只按这条口径判断：不要因为你自己认为数学上应当更严格（例如口径只要最终答案，你却要求完整证明）而提高要求，"
            "也不要放宽。口径之外你认为值得知道的事，如实写进“限制／未验证的部分”，但不要因此改变状态或评价。")


def lang_block(lang):
    """2.5：输出语言。固定取值（得出/硬伤/通过 等）和 JSON 键名始终不变；答案用 LaTeX；代码与题面不翻译。"""
    lang = (lang or "中文").strip()
    if lang in ("中文", "", "zh"):
        return "\n【输出语言】推导、验证方案、意见、未得出的理由、各类说明一律用中文写。"
    return (f"\n【输出语言 / Output language: {lang}】请用 {lang} 写推导（reasoning）、验证方案的文字、意见、未得出的理由和各类说明。"
            "但这些保持原样不翻译：JSON 的键名；固定取值（如 得出/部分得出/未得出、硬伤/次要、通过/有问题/不确定、采纳/驳回、"
            "一致/不一致、同路线复现/另一方法/未能换方法 等，程序按这些中文取值识别）；answer 里的 LaTeX 写法；代码里的变量名和注释（用英文）；题面原文。")


SYSTEM_PRODUCER = """你是多模型互查流程中的一名【生产者】：独立解一道数学题，并给出自己的验证。
规则：
1. 你看不到其他生产者，也不要猜他们的答案；只对自己的结果负责。
2. 先如实判断自己得没得出答案，status 三选一：得出（有完整答案）／部分得出（确实没有完整答案，只确定了一部分，例如某些取值范围内的结论）／未得出（不知道）。【不知道就写未得出：编造答案比不答更糟，如实说不知道不会被扣分，也不会被打回。】但 status 不是"得出"时必须写 unknown：tried＝试过什么，stuck＝具体卡在哪一步，missing＝缺什么才能继续；要具体，不能敷衍。\n2a. 【status 看口径，不看你自己的标准】题目后面会给出【口径】（什么算做完）。口径只要最终答案时，你对最终答案有把握就写“得出”；推导里某一步没有严格证明、引用了没有核对过出处的定理或文献，都如实写在 limits 里（这些会作为“未验证的部分”随结论一起报告），不要因此把 status 降成部分得出。“部分得出”只用于确实没有完整答案。你只负责如实报告：做了什么、哪些没验证；“得出”与否最终按口径和程序规则判定，不由你自己另立的标准决定。
3. answer（status＝得出时必填）只写最终答案本身（数、表达式、集合、区间等），写成最简标准形式，不带解释。请尽量用 LaTeX 写法：根号 \\sqrt{2}，分数 \\frac{a}{b}，圆周率 \\pi，乘方 x^{2}，无穷 \\infty，区间 (a, b]、[a, b) 并注意开闭，多个答案用逗号分隔或 \\{ \\}；不要用 Unicode 数学符号（√ π ² 等）、sqrt(2) 这类程序写法，也不要加 $ 或 \\boxed（写错了程序会尽量自动整理，整理不了会影响自动比对）。
3a. 【精确值】除非题目明确要求给近似值，否则一律给精确值（如 \\sqrt{2}、\\frac{1}{3}、\\pi），不要写成 1.414、0.333 这样的小数近似；小数近似与精确答案不等价，会被判错。若题目要求近似，按题目要求的精度写。
3b. sympy（可选，建议填）：用 Python 写法把同一个答案再写一遍，供程序数值验证：sqrt(2)*pi、Rational(1,3)、2**10、exp(1)；可以含变量。不能用 ^ 表示乘方。
3c. segments（可选）：答案随条件分段、或只确定了一部分时，逐段写：condition＝适用范围（如 p<2），value＝该范围内的值（LaTeX），state＝确定／未确定。
4. reasoning 写完整的关键推导，要具体、可检查；不设字数上限，该写多少写多少，但不要灌水。
5. verify_plan 是用文字写的验证方案，供别人照着独立实现，所以要写清楚：what＝验证的对象和统计量/公式的【精确定义】（定义不同会得出不同的数）；method＝怎么验证；expected＝你预期看到什么结果；tolerance＝事先声明的容差（没有数值比较写"无"）。status 是"未得出"时可以省略。
6. code 是你自己的验证代码（可以为空字符串）。程序会运行它并把结果存档给审查者。【把你预期的值写成 assert】（例如 assert result == 3, result），不要只 print 出来再靠肉眼对照；断言失败（或代码出错退出）就表示你自己的验证没有通过——这时要弄清是代码、预期还是答案有问题，弄不清就如实写进 limits，不要若无其事地提交。程序会在报告里标出“作者自己的验证没有通过却提交了答案”。验证不到的部分写在 limits 里，如实写"只验证了小情形"之类，不要夸大。
7. 只输出一个 JSON 对象，不要输出其他文字。"""

SCHEMA_SUBMIT = ('{"status": "得出 / 部分得出 / 未得出（三选一）", "answer": "（status 为得出时必填，LaTeX）", '
                 '"sympy": "（可选）", "segments": [{"condition": "...", "value": "...", "state": "确定 / 未确定"}], '
                 '"unknown": {"tried": "试过什么", "stuck": "卡在哪", "missing": "缺什么"}, "reasoning": "...", '
                 '"verify_plan": {"what": "...", "method": "...", "expected": "...", "tolerance": "..."}, '
                 '"code": "...", "limits": "..."}\n'
                 '说明：unknown 在 status 不是"得出"时必填，status 是"得出"时可省略；segments 可省略；'
                 'status 是"未得出"时 answer、verify_plan、code 都可以省略。')
SCHEMA_REVISE = ('{"responses": [{"opinion": "意见编号", "decision": "采纳 或 驳回", "check": "你是怎么核实的"}], '
                 '"changes": [{"layer": "（选填）", "what": "改了什么"}], '
                 '"status": "得出 / 部分得出 / 未得出", "answer": "...", "sympy": "...", "segments": [...], "unknown": {...}, '
                 '"reasoning": "...", "verify_plan": {...同上...}, "code": "...", "limits": "..."}')

SYSTEM_REVISER = SYSTEM_PRODUCER + """
8. 这是修改轮：审查者给了你一些意见（只涉及你自己的提交）。【先核实，再决定】：每条意见都要你自己重算或重新检查一遍，再写采纳或驳回，check 里写清楚你怎么核实的、得到了什么（有具体数值或步骤，不能只写"同意"或"不同意"）。目的不是让谁成为权威，而是把正确的结果找出来：审查者可能对，也可能错；你的原答案可能对，也可能错。核实后发现自己确实没有完整答案，可以把 status 改成"部分得出"或"未得出"（不要仅仅因为某一步没有严格证明就降级，口径没要求的不算）；原来没得出、现在得出了，也可以改成"得出"。意见分两种：被反驳的硬伤（有反例、自相矛盾、计算错误或与口径冲突）必须处理；次要意见酌情处理。
9. 每条意见都必须有一条 responses，opinion 填意见编号。
10. 如果你改了答案、方法、范围或验证，changes 里写明改了什么（layer 选填，写你自己的分类即可）；没有任何改动则 changes 写 []。"""


def author_check(status, run):
    """2.5：作者自己的验证有没有通过。验证代码要求写成 assert：断言失败或代码出错退出（状态不是"完成"），
    而作者仍然提交了答案（状态不是未得出）→ 标出。没有运行（没写代码/没开运行）不算失败，只是没验证。"""
    if not run or status == "未得出":
        return {"failed": False, "status": (run or {}).get("status") or "未运行", "tail": ""}
    bad = run.get("status") != "完成"
    err = (run.get("stderr") or "").strip().splitlines()
    tail = (err[-1] if err else (run.get("stdout") or "").strip().splitlines()[-1:] and "" ) if bad else ""
    return {"failed": bad, "status": run.get("status"), "tail": (tail or "")[:300]}


def status_of(sub):
    return (sub or {}).get("status") or "得出"


def answer_text(sub):
    """一份提交的"答案"在显示和比较里的写法：得出＝answer；部分得出＝已确定的部分；未得出＝（未得出）。"""
    st = status_of(sub)
    if st == "未得出":
        return "（未得出）"
    if st == "部分得出":
        a = sub.get("answer") or ""
        return f"（部分得出）{a}" if a else "（部分得出）"
    return sub.get("answer") or ""


def segments_text(sub):
    segs = sub.get("segments") or []
    return "；".join(f"{s['condition']} → {s['value']}（{s['state']}）" for s in segs)


def unknown_text(sub):
    u = sub.get("unknown") or {}
    if not u:
        return ""
    return "试过：" + (u.get("tried") or "—") + "；卡在：" + (u.get("stuck") or "—") + "；缺：" + (u.get("missing") or "—")


def sub_block(sub):
    """一份提交的正文（给生产者自己的历史、给审查者看都用这个写法）。"""
    st = status_of(sub)
    s = f"状态：{st}"
    if st != "未得出":
        s += f"\n答案：{sub.get('answer') or '（无完整答案）'}"
    if sub.get("segments"):
        s += f"\n分段：{segments_text(sub)}"
    if st != "得出":
        s += f"\n未解决的部分：{unknown_text(sub)}"
    s += f"\n推导：{sub.get('reasoning') or ''}"
    return s


def solve_prompt(it, libs, allow_code, scope=None, lang=None):
    return (f"题目：\n{it['problem']}\n{scope_block(scope)}{lang_block(lang)}\n\n{libs_line(libs, allow_code)}"
            f"\n\n按下面的格式输出 JSON：\n{SCHEMA_SUBMIT}")


def _round_record(k, rec):
    """历史里的一轮：这一轮它的提交、收到的意见、审查者对这份提交的独立验证结论。"""
    sub = rec["sub"]
    p = [f"\n======== 第 {k} 轮 ========"]
    if sub.get("carried"):
        p.append(f"（这一轮你没有收到意见，沿用上一轮的提交；状态：{status_of(sub)}；答案：{answer_text(sub)}）")
    else:
        p.append(sub_block(sub))
        p.append(f"验证方案：{json.dumps(sub['verify_plan'], ensure_ascii=False)}")
        p.append(f"你的验证代码：\n{sub['code'] or '（无）'}\n程序运行它的结果：{fmt_run(sub.get('run'))}")
        p.append(f"你声明的局限：{sub.get('limits') or '（无）'}")
        if sub.get("responses"):
            p.append("你对上一轮意见的回应：" + "".join(
                f"\n  - 意见 {r['opinion']}：{r['decision']}；核实：{r['check']}" for r in sub["responses"]))
        if sub.get("changes"):
            p.append("你声明的改动：" + "；".join(f"{(c.get('layer') + '：') if c.get('layer') else ''}{c['what']}" for c in sub["changes"]))
    for v in rec.get("ver", []):
        t = f"审查者{v['reviewer_label']}对这份提交的独立验证：{v['result']}"
        if v.get("diagnosis") and v["diagnosis"] != "无":
            t += f"（诊断：{v['diagnosis']}）"
        t += f"；理由：{v.get('reason') or '（无）'}"
        if v.get("impl_code"):
            t += f"\n  审查者照你的验证方案写的实现（{v.get('impl_note') or ''}）：\n{v['impl_code']}\n  它的运行结果：{fmt_run(v.get('impl_run'))}"
        p.append(t)
    if rec.get("ops"):
        p.append("这一轮审查者发给你的意见：" + "".join(
            f"\n  - 意见 {o['id']}（{o['severity']}{'／' + o['layer'] if o.get('layer') else ''}／位置：{o['location']}）：{o['problem']}"
            for o in rec["ops"]))
    return "\n".join(p)


def revise_prompt(it, hist, ops, libs, allow_code, scope=None, lang=None):
    """hist：此前若干轮的记录（按轮次从早到晚，至少含上一轮）；ops：本次要逐条回应的意见（即最后一轮的意见）。"""
    p = [f"题目：\n{it['problem']}\n{scope_block(scope)}{lang_block(lang)}",
         "\n下面是你在这道题上此前的记录（只涉及你自己，没有其他生产者的信息）："]
    for i, (k, rec) in enumerate(hist):
        if i == len(hist) - 1:  # 最后一轮的意见在下面单独列出，这里不重复
            rec = dict(rec, ops=[])
        p.append(_round_record(k, rec))
    if not ops:  # 2.9：没有针对你的意见，是因为用户补充了信息而重新作答
        p.append("\n======== 这一轮没有针对你的意见 ========\n用户在流程中补充了信息（见最前面的【用户补充】）。"
                 "按补充后的题目重新作答；答案或状态有变化的，在 changes 里写明。responses 写 []。")
    else:
        p.append("\n======== 现在需要你逐条回应的意见（即上面最后一轮的意见） ========")
    for o in ops:
        lay = f"；角度：{o['layer']}" if o.get("layer") else ""
        bas = f"（被反驳的依据：{o['basis']}）" if o.get("basis") else ""
        p.append(f"- 意见编号：{o['id']}\n  严重程度：{o['severity']}{bas}{lay}；位置：{o['location']}\n  内容：{o['problem']}")
    p.append(f"\n{libs_line(libs, allow_code)}\n\n逐条核实后，按下面的格式输出 JSON：\n{SCHEMA_REVISE}")
    return "\n".join(p)


def fmt_run(run):
    if not run:
        return "（没有运行：未允许运行代码，或没有写代码）"
    s = f"{run['status']}，用时 {run.get('seconds')} 秒"
    if run.get("stdout"):
        s += f"\n输出：\n{run['stdout'].rstrip()}"
    if run.get("stderr"):
        s += f"\n错误信息：\n{run['stderr'].rstrip()[-1500:]}"
    if run.get("truncated"):
        s += "\n（输出过长，已截断）"
    return s


SYSTEM_REVIEWER = """你是多模型互查流程中的【审查者】，像一位批改作业的老师：你不做题，也不要重新解一遍题，你的价值是用不同的视角比对，避开生产者因为自己的推理过程而走不出去的错误。
你看到的是几位生产者各自独立的提交（用甲、乙、丙标注）。检查时可以参考下面四个角度（只是帮你想问题的提示，不是必须套用的分类，意见里的 layer 写这四个或你自己的说法都行，也可以不写）：
- 表示层：对题目的理解是否正确；答案的写法是否合规（给了小数近似却没有题目要求，例如写 1.414 而不是 \\sqrt{2}，也是表示层错误——小数近似与精确值不等价）。题目问的是 a，是否被转成了求 b，a 和 b 是否一样？（例：题目问"正规性"，却只按个位数字 0–4 设计，没考虑 00、11、22 等多位情形，这就是表示层错误。）
- 方法层：解法或数学模型是否适合这道题；几位生产者是否用了不同的路线（所有人走同一条路，要指出）。
- 约束层：验证范围、取值范围、前提条件是否与题目一致（题目是 0–1，是否被做成了 1–2 或 0–2）；题目若明确规定了答案的形式或精度（如"精确到小数点后两位""用分数表示"），答案不符合也是约束层错误。
- 验证层：计算是否算错（如 1+1=3）；验证代码本身出错（调用了不存在的函数等）也算在这里。
除此之外，还要查三类：①同一份提交前后自相矛盾；②自己写下的警告或前提在后面没有执行；③数值对了但挂在错误的对象上。
关于"未得出／部分得出"：生产者可以如实说自己没得出答案，这是允许的，不要因此挑它的毛病。你要检查的是它的理由成不成立：它真的试过吗？说的卡点是真的卡点，还是稍加努力就能越过去？如果你能具体指出卡点怎么越过、或"部分得出"里其实已经可以确定更多，就写一条意见（severity 写硬伤，basis 写"弃权理由不成立"，problem 里写出具体的越过办法）；说不出具体办法、只是觉得它放弃得太早，不要写硬伤。理由成立就评为"通过"。不要替它做题，也不要在意见里写出其他生产者的答案。
【严格度只有一把尺子：口径】题目后面给出的【口径】是题目提出者规定的"什么算做完"。你只按它判断：不要因为你自己认为数学上应当更严格（比如口径只要最终答案，你却要求证明上界、要求完整证明）就把它当硬伤；也不要放宽。口径之外你认为值得知道的事，写进 unverified_parts，不要写成硬伤，也不要因此改变总体评价。
【意见分两类，不要混】
(1) 硬伤＝被反驳了：你能指出具体的反例、自相矛盾、计算错误，或者与口径冲突，其中之一；把类别写在 basis（只能写：反例／自相矛盾／计算错误／与口径冲突／弃权理由不成立），并在 problem 里写出反驳的具体内容（反例本身、自相矛盾的两处、错在哪一步、与口径哪一句冲突）。硬伤必须被处理，没处理就不能闭合。
(2) 未验证的部分＝没被反驳，但没有被证明或没有被验证到（例如某个结论只验证了小情形；引用了某个定理或文献，生产者自己没有证明、你也没有独立核对出处）。这类【不是硬伤，不挡闭合】，写进 unverified_parts：target＝对象；what＝哪个结论或步骤没有被证明或验证；tried＝你试过什么去反驳它、为什么没找到反例（必填，要具体，如"对 n≤8 全部枚举，没找到反例"；如果你什么都没试，就如实写"没有试"，不要因为没试就把它升级成硬伤）。引用了定理或文献但没有证明、你也没有独立核对出处的，一律归入这里，不要因为"听起来是对的"就当作已经通过。
没有证明不等于错误：只有真的被反驳了才叫硬伤。次要＝措辞、写法等不影响结论的小问题。
规则：
1. 每条意见必填：target（对象，甲/乙…）、severity（硬伤 或 次要）、problem（问题，写给该生产者本人看的，只谈它自己的提交，【不要提到其他生产者、不要写出他们的答案】）；severity 是硬伤时还必须写 basis。选填：layer、location（位置）、internal_note（需要对比其他生产者时写在这里，不会发给生产者）。
2. 对象的总体评价：通过＝没有被反驳（可以有未验证的部分，写在 unverified_parts）；有问题＝有被反驳的硬伤（必须至少有一条该对象的硬伤意见）；不确定＝你自己没法判断（例如材料残缺读不懂）：它不挡闭合，但程序会把它记进报告的"未验证的部分"；不要把"没证明"写成不确定，那请写进 unverified_parts（what、tried）。没有发现问题就写"通过"，不要为了凑数挑毛病。
3. 不要因为多数人一致就认为对；一致也可能是共同的表示层错误。判断各生产者的路线是否本质相同，写在 same_route。
4. 独立验证：对每位生产者（未得出的除外），只依据它【文字写的 verify_plan】（你看不到它的代码），自己写一份验证实现，放在 implementations 里；如果它的方案无法验证或太含糊，就不写，并在 unverified_parts 里说明。你的实现是为了验证它的结果，不是替它解题；只用你确定存在的函数。每份实现必填 check_type（核对类型）：同路线复现＝按它的方案、同一个模型假设写的实现，只能发现实现写错了，核对不了模型本身对不对；另一方法＝你用与它不同的方法算同一个量，在 check_note 里写明用的什么方法；未能换方法＝只能同路线，在 check_note 里写明原因。能换方法就尽量换：独立到建模假设那一层才算真正独立核对。
5. 只有当所有生产者都没有可执行的验证方案时，才可以在 fallback_answer 里写你独立求得的答案，否则留空。
6. 只输出一个 JSON 对象，不要输出其他文字。"""


def schema_review(labels):
    ov = ", ".join(f'"{l}": "通过 / 有问题 / 不确定"' for l in labels)
    return ('{"overall": {' + ov + '}, "same_route": true 或 false, '
            '"answers_equivalent": "是 / 否 / 不确定（程序拿不准各答案是否等价时才需要认真填）", '
            '"opinions": [{"target": "' + labels[0] + '", "severity": "硬伤 / 次要", '
            '"basis": "（severity 为硬伤时必填）反例 / 自相矛盾 / 计算错误 / 与口径冲突 / 弃权理由不成立", '
            '"problem": "（必填）写给该生产者的问题，硬伤要写出具体反驳内容", '
            '"layer": "（选填）", "location": "（选填）", "internal_note": "（选填）"}], '
            '"unverified_parts": [{"target": "' + labels[0] + '", "what": "哪个结论或步骤没有被证明或验证", '
            '"tried": "你试过什么去反驳它、为什么没找到反例（必填）"}], '
            '"implementations": [{"target": "' + labels[0] + '", "check_type": "同路线复现 / 另一方法 / 未能换方法", '
            '"check_note": "（另一方法写明用的什么方法；未能换方法写明原因）", '
            '"explain": "照它的描述我这样实现", "code": "..."}], '
            '"fallback_answer": ""}\n'
            '说明：opinions、unverified_parts 没有就写 []；opinions 每条必填 target、severity、problem（硬伤另须 basis），其余选填；'
            'target 只能是：' + "、".join(labels) + '。')


def review_prompt(it, views, facts, libs, allow_code, scope=None, lang=None):
    p = [f"题目：\n{it['problem']}\n{scope_block(scope)}{lang_block(lang)}", "\n各生产者的提交："]
    labels = [v["label"] for v in views]
    for v in views:
        s = f"\n【生产者{v['label']}】\n{sub_block(v)}\n验证方案：{json.dumps(v['verify_plan'], ensure_ascii=False)}\n它声明的局限：{v.get('limits') or '（无）'}"
        if v.get("responses"):
            s += "\n它对上一轮意见的回应："
            for r in v["responses"]:
                s += f"\n  - 意见「{r['opinion_text']}」→ {r['decision']}；核实：{r['check']}"
        if v.get("changes"):
            s += "\n它声明的改动：" + "；".join(f"{(c.get('layer') + '：') if c.get('layer') else ''}{c['what']}" for c in v["changes"])
        if v.get("author_ran") is not None and v.get("status", "得出") != "未得出":
            s += f"\n（程序记录：它的验证代码{'已运行' if v['author_ran'] else '没有运行'}；结果稍后单独给你，写你自己的实现时先不要看。）"
        p.append(s)
    p.append(f"\n程序对各答案的基础比对（只做了格式规范化和数值验证）：{facts or '只有一位生产者'}")
    p.append(f"\n{libs_line(libs, allow_code)}（这句话也适用于你写的实现。）\n\n按下面的格式输出 JSON：\n{schema_review(labels)}")
    return "\n".join(p)


SYSTEM_VERIFIER = """你是【审查者】，正在做独立验证的最后一步：比较生产者自己的验证结果和你独立实现的结果。
规则：
1. 比较要在生产者事先声明的容差内进行；统计性结果是"统计上相容"，不是逐位相等。
2. 两边不一致时先诊断，再下结论，diagnosis 只能是：采样不足（或精度不够）、定义不同（两边验证的不是同一个量）、真有错、无法判断、无。
3. result 只能是：一致、不一致、部分验证（只验证了一部分，如只验证了小情形）、未能验证。
4. 结果为"不一致"的对象，必须在 opinions 里写出针对它的意见（problem 只谈它自己的提交，不要提到其他生产者或他们的答案）；意见必填 target、severity、problem，其余选填；severity 写硬伤时 basis 可以不写（程序会记为"验证结果不一致"）。
5. unverified 列出这次【没有实际验证】或不确定的地方，要具体，不要写"无"来敷衍；确实都验证了才写空列表。注意核对类型：你的实现如果是"同路线复现"，两边一致只说明实现没写错，没有核对模型假设本身，请在 unverified 里写明哪些假设没有被另外核对。没被反驳、只是没验证到的地方属于"未验证"，不要写成不一致，也不要写成硬伤。
6. 代码出错如果是因为调用了不存在的函数（环境问题或凭印象写错），这说明这次验证没有完成，result 写"未能验证"，不要据此判定生产者的答案有错。
7. 只输出一个 JSON 对象，不要输出其他文字。"""


def schema_verify(labels, targets):
    vs = ", ".join('{"target": "' + t + '", "result": "一致 / 不一致 / 部分验证 / 未能验证", '
                   '"diagnosis": "采样不足 / 定义不同 / 真有错 / 无法判断 / 无", "reason": "..."}' for t in targets)
    return ('{"verifications": [' + vs + '], '
            '"opinions": [{"target": "' + labels[0] + '", "severity": "硬伤 / 次要", "problem": "（必填）", '
            '"layer": "（选填）", "location": "（选填）", "internal_note": "（选填）"}], "unverified": ["..."]}\n'
            '说明：verifications 里每个对象各一条（已按这道题的对象预先列好，照填即可）；opinions 没有就写 []。')


def verify_prompt(it, rows, labels=None, scope=None, lang=None):
    p = [f"题目：\n{it['problem']}\n{scope_block(scope)}{lang_block(lang)}"]
    for r in rows:
        ct = f"\n你声明的核对类型：{r['check_type']}" + (f"（{r['check_note']}）" if r.get("check_note") else "") if r.get("check_type") else ""
        p.append(f"\n【生产者{r['label']}】\n它声明的验证方案：{json.dumps(r['verify_plan'], ensure_ascii=False)}\n"
                 f"它的验证代码的运行结果：{fmt_run(r['author_run'])}\n"
                 f"你的独立实现（{r['impl_note']}）的运行结果：{fmt_run(r['impl_run'])}{ct}")
    labs = labels or [r["label"] for r in rows]
    p.append(f"\n按下面的格式输出 JSON：\n{schema_verify(labs, [r['label'] for r in rows])}")
    return "\n".join(p)


# ---------------------------------------------------------------- 校验（2.4：格式类一律宽收，只在真的无法使用时才打回）

def _s(x):
    return x.strip() if isinstance(x, str) else ""


_PLAIN = re.compile(r"(?<![\\A-Za-z])(sqrt|pi|inf|infinity|exp|sin|cos|tan|log|ln)\s*\(|(?<![\\A-Za-z])pi(?![A-Za-z])|\*\*")


def _tidy_answer(a, warns):
    """答案整理：去掉 $、\\boxed，Unicode 符号转 LaTeX；记录做了什么，不打回。"""
    from ..fields import mathcheck as MC
    raw = a
    a = a.replace("$", "")
    m = re.fullmatch(r"\s*\\boxed\{(.*)\}\s*", a, flags=re.S)
    if m:
        a = m.group(1)
    if MC.has_unicode_math(a):
        a = MC.uni_to_latex(a).strip()
    if a != raw:
        warns.append(f"答案写法已由程序整理：「{raw}」→「{a}」")
    if _PLAIN.search(a):
        warns.append("答案里有 sqrt(2)、pi、** 这类程序写法，程序无法确认含义，自动比对会退回 sympy 字段或交人工")
    return a


def _check_sympy(obj, a, warns):
    """sympy 字段只是辅助：解析不了、与答案对不上都只记警告，不打回。"""
    from ..fields import mathcheck as MC
    py = obj.get("sympy")
    py = py.strip() if isinstance(py, str) else ""
    obj["sympy"] = py
    if not py:
        return
    try:
        pn = MC.parse_py(py)
        MC._check_eval(pn)
    except MC.MathError as e:
        warns.append(f"sympy 字段程序读不了（{e}），已忽略")
        obj["sympy"] = ""
        return
    try:
        ln = MC.parse_latex(a)
        if ln[0] == "num" and pn[0] == "num" and not MC.struct_equal(ln, pn):
            warns.append("答案与 sympy 字段数值不一致：两种写法可能有一个写错，或程序没读懂 LaTeX，请人工看一眼")
    except MC.MathError:
        return


def _clean_segments(obj, warns):
    segs = obj.get("segments")
    out = []
    if isinstance(segs, list):
        for s in segs:
            if isinstance(s, dict) and _s(s.get("condition")) and _s(s.get("value")):
                state = "确定" if _s(s.get("state")) == "确定" else "未确定"
                out.append({"condition": _s(s["condition"]), "value": _s(s["value"]), "state": state})
    elif segs not in (None, "", []):
        warns.append("segments 格式不对，已忽略")
    obj["segments"] = out


def _clean_unknown(obj):
    u = obj.get("unknown")
    u = u if isinstance(u, dict) else {}
    return {"tried": _s(u.get("tried")), "stuck": _s(u.get("stuck")), "missing": _s(u.get("missing"))}


def validate_submission(expected_opinions=None):
    """生产者提交的校验；expected_opinions 不为 None 时（修改轮）要求每条意见都有回应。"""
    def v(obj):
        if not isinstance(obj, dict):
            raise FormatError("返回的不是 JSON 对象")
        warns = []
        st = _s(obj.get("status"))
        if st not in STATUSES:
            st = "得出" if normalize(obj.get("answer", "")) else ""
        if not st:
            raise FormatError("缺少 status（得出 / 部分得出 / 未得出）：既没有写 status，也没有给出 answer。"
                              "如果你不知道答案，请把 status 写成 未得出，并在 unknown 里写清楚试过什么、卡在哪、缺什么")
        obj["status"] = st
        _clean_segments(obj, warns)
        obj["answer"] = _s(obj.get("answer"))
        u = _clean_unknown(obj)
        obj["unknown"] = u
        if st == "得出":
            if not normalize(obj["answer"]):
                raise FormatError("status 是 得出，但 answer 为空；没有完整答案请把 status 改成 部分得出 或 未得出")
        elif st == "部分得出":
            if not obj["answer"] and not any(s["state"] == "确定" for s in obj["segments"]):
                raise FormatError("status 是 部分得出，需要在 answer 里写已确定的部分，或在 segments 里至少写一段状态为 确定 的")
            if len(u["stuck"]) < 4:
                raise FormatError("部分得出 需要在 unknown.stuck 里写清楚哪一部分没确定、卡在哪")
        else:  # 未得出
            if len(u["tried"]) < 4 or len(u["stuck"]) < 4:
                raise FormatError("未得出 需要在 unknown 里写 tried（试过什么）和 stuck（卡在哪一步），要具体；"
                                  "这不是惩罚，只是让审查者能判断你的理由是否成立")
            obj["answer"] = ""
            obj["segments"] = []
        if st != "未得出" and obj["answer"]:
            obj["answer"] = _tidy_answer(obj["answer"], warns)
            _check_sympy(obj, obj["answer"], warns)
        else:
            obj["sympy"] = ""
        obj["reasoning"] = _s(obj.get("reasoning", ""))
        vp = obj.get("verify_plan")
        vp = vp if isinstance(vp, dict) else {}
        if st != "未得出":
            miss = [k for k in ("what", "method", "expected", "tolerance") if not _s(vp.get(k))]
            if miss:
                warns.append("verify_plan 缺：" + "、".join(miss) + "（已填「未写明」）")
        obj["verify_plan"] = {k: (_s(vp.get(k)) or "（未写明）") for k in ("what", "method", "expected", "tolerance")} \
            if st != "未得出" else {k: _s(vp.get(k)) for k in ("what", "method", "expected", "tolerance")}
        if not isinstance(obj.get("code", ""), str):
            obj["code"] = ""
        obj["code"] = obj.get("code") or ""
        obj["limits"] = _s(obj.get("limits"))
        if expected_opinions is not None:
            rs = obj.get("responses")
            if not isinstance(rs, list):
                if expected_opinions:
                    raise FormatError("修改轮必须有 responses 列表，每条意见一项")
                rs = obj["responses"] = []  # 2.9：没有意见要回应（因用户补充而重新作答）时可以不写
            seen = {}
            for i, r in enumerate(rs, 1):
                if not isinstance(r, dict) or r.get("decision") not in ("采纳", "驳回"):
                    raise FormatError(f"responses 第 {i} 项：decision 只能是 采纳 或 驳回（实际是「{r.get('decision') if isinstance(r, dict) else r}」），"
                                      "每项要有 opinion、decision、check")
                if len(_s(r.get("check"))) < 8:
                    raise FormatError(f"responses 第 {i} 项：check 要写明你怎么核实的（有具体步骤或数值），不能只写同意或不同意")
                key = str(r.get("opinion")).split("｜")[0].strip()
                seen[key] = {"opinion": key, "decision": r["decision"], "check": _s(r["check"])}
            missing = [o for o in expected_opinions if o not in seen]
            if missing:
                raise FormatError("这些意见没有回应：" + "、".join(missing))
            obj["responses"] = [seen[o] for o in expected_opinions]
            ch = obj.get("changes", [])
            ch = ch if isinstance(ch, list) else []
            obj["changes"] = [{"layer": _s(c.get("layer")), "what": _s(c.get("what"))} for c in ch
                              if isinstance(c, dict) and _s(c.get("what"))]
        else:
            obj["responses"], obj["changes"] = [], []
        obj["warnings"] = warns
    return v


_SEV_SOFT = ("次", "小", "轻", "minor")


def _norm_basis(b):
    """只认"恰好指明一种"的写法；把模板里的整串选项原样抄回来（命中多个）不算数。"""
    b = _s(b)
    hit = [x for x in BASES if x in b]
    return hit[0] if len(hit) == 1 else ""


def _check_opinions(obj, labels, key="opinions", need_for=(), basis_default=None):
    """basis_default：审查者验证步骤里写的"不一致"意见，缺 basis 时记为这个值（验证结果不一致本身就是被反驳）。"""
    ops = obj.get(key, [])
    if ops in (None, ""):
        ops = []
    if not isinstance(ops, list):
        raise FormatError(f"{key} 必须是列表（没有意见就写 []）")
    clean = []
    for i, o in enumerate(ops, 1):
        if not isinstance(o, dict):
            raise FormatError(f"{key} 第 {i} 项不是对象")
        if o.get("target") not in labels:
            raise FormatError(f"{key} 第 {i} 项：target 写的是「{o.get('target')}」，只能是 {'、'.join(labels)} 之一")
        if not _s(o.get("problem")):
            raise FormatError(f"{key} 第 {i} 项：缺少 problem（写给该生产者的问题描述）")
        sev = _s(o.get("severity"))
        if sev not in SEVERITY:  # 写得不标准时按保守处理：含"次/小/轻"算次要，其余一律按硬伤，不打回
            sev = "次要" if any(w in sev.lower() for w in _SEV_SOFT) else "硬伤"
        basis = ""
        if sev == "硬伤":
            basis = _norm_basis(o.get("basis")) or basis_default or ""
            if not basis:
                raise FormatError(
                    f"{key} 第 {i} 项：severity 是硬伤，必须在 basis 写明被反驳的依据（只能是 "
                    f"{'／'.join(BASES[:5])} 之一），并在 problem 里写出具体的反驳内容；"
                    "如果只是没有被证明、没有验证到、没有核对出处，那不是硬伤——请把 severity 改成次要，"
                    "或者把它从 opinions 里拿掉、写进 unverified_parts（写明 tried：你试过什么、没找到反例）")
        clean.append({"target": o["target"], "layer": _s(o.get("layer")), "severity": sev, "basis": basis,
                      "location": _s(o.get("location")) or "（未指明）", "problem": _s(o["problem"]),
                      "internal_note": _s(o.get("internal_note"))})
    for t in need_for:
        if not any(c["target"] == t for c in clean):
            raise FormatError(f"对象 {t} 被评为有问题/不确定或验证不一致，必须至少写一条 target 为 {t} 的意见")
    obj[key] = clean


def validate_review(labels):
    def v(obj):
        if not isinstance(obj, dict) or not isinstance(obj.get("overall"), dict):
            raise FormatError("缺少 overall（对每个对象的总体评价，如 {\"甲\": \"通过\"}）")
        for l in labels:
            if obj["overall"].get(l) not in OVERALL:
                raise FormatError(f"overall.{l} 写的是「{obj['overall'].get(l)}」，只能是 通过/有问题/不确定")
        obj["same_route"] = bool(obj.get("same_route")) if obj.get("same_route") in (True, False) else False
        if obj.get("answers_equivalent") not in ("是", "否", "不确定"):
            obj["answers_equivalent"] = "不确定"
        _check_opinions(obj, labels, need_for=[l for l in labels if obj["overall"][l] == "有问题"])
        for l in labels:  # 2.5："有问题"＝被反驳，必须有一条写明依据的硬伤；没证明/没验证到不是"有问题"
            if obj["overall"][l] == "有问题" and not any(o["target"] == l and o["severity"] == "硬伤" for o in obj["opinions"]):
                raise FormatError(f"对象 {l} 被评为有问题，但没有一条 severity 为硬伤的意见：只有被反驳（反例／自相矛盾／计算错误／"
                                  "与口径冲突）才能评为有问题，并且必须有写明 basis 的硬伤意见；如果只是没有证明、没有验证到，"
                                  "请评为通过，把它写进 unverified_parts（what 和 tried）")
        up = obj.get("unverified_parts", [])
        if up in (None, ""):
            up = []
        if not isinstance(up, list):
            raise FormatError("unverified_parts 必须是列表（没有就写 []）")
        parts = []
        for i, u in enumerate(up, 1):
            if not isinstance(u, dict) or not _s(u.get("what")):
                raise FormatError(f"unverified_parts 第 {i} 项：缺少 what（哪个结论或步骤没有被证明或验证）")
            if len(_s(u.get("tried"))) < 2:
                raise FormatError(f"unverified_parts 第 {i} 项：缺少 tried（你试过什么去反驳它、为什么没找到反例；"
                                  "没试就如实写“没有试”）。这是把它降为“未验证”而不是硬伤的依据，必须写")
            t = u.get("target") if u.get("target") in labels else "整体"
            parts.append({"target": t, "what": _s(u["what"]), "tried": _s(u["tried"])})
        obj["unverified_parts"] = parts
        impls = obj.get("implementations", [])
        if not isinstance(impls, list):
            impls = []
        clean_impls = []
        for i, im in enumerate(impls, 1):
            if not (isinstance(im, dict) and im.get("target") in labels and isinstance(im.get("code"), str)
                    and im["code"].strip()):
                continue
            ct = _s(im.get("check_type"))
            hit = [c for c in CHECK_TYPES if c in ct]
            ct = hit[0] if len(hit) == 1 else ""  # 把模板里的整串选项原样抄回来（命中多个）不算数
            if not ct:
                raise FormatError(f"implementations 第 {i} 项：缺少 check_type（核对类型），只能是 "
                                  f"{'／'.join(CHECK_TYPES)}")
            note = _s(im.get("check_note"))
            if ct != "同路线复现" and len(note) < 4:
                raise FormatError(f"implementations 第 {i} 项：check_type 是 {ct}，check_note 必须写明"
                                  + ("用的什么方法" if ct == "另一方法" else "为什么没能换方法"))
            clean_impls.append({"target": im["target"], "explain": _s(im.get("explain")), "code": im["code"],
                                "check_type": ct, "check_note": note})
        obj["implementations"] = clean_impls
        obj["fallback_answer"] = _s(obj.get("fallback_answer"))
    return v


RESULTS = ("一致", "不一致", "部分验证", "未能验证")
DIAGS = ("采样不足", "定义不同", "真有错", "无法判断", "无")


def validate_verify(labels, targets):
    def v(obj):
        if not isinstance(obj, dict) or not isinstance(obj.get("verifications"), list):
            raise FormatError("缺少 verifications 列表")
        got = {}
        for i, x in enumerate(obj["verifications"], 1):
            if not isinstance(x, dict) or x.get("target") not in targets:
                raise FormatError(f"verifications 第 {i} 项：target 只能是 {'、'.join(targets)} 之一")
            if x.get("result") not in RESULTS:
                raise FormatError(f"verifications 第 {i} 项：result 写的是「{x.get('result')}」，只能是 {'/'.join(RESULTS)}")
            diag = x.get("diagnosis") if x.get("diagnosis") in DIAGS else "无"
            got[x["target"]] = {"target": x["target"], "result": x["result"], "diagnosis": diag,
                                "reason": _s(x.get("reason"))}
        miss = [t for t in targets if t not in got]
        if miss:
            raise FormatError("这些对象没有验证结论：" + "、".join(miss))
        obj["verifications"] = [got[t] for t in targets]
        _check_opinions(obj, labels, need_for=[t for t in targets if got[t]["result"] == "不一致"],
                        basis_default="验证结果不一致")
        un = obj.get("unverified", [])
        obj["unverified"] = [_s(u) for u in un if _s(u)] if isinstance(un, list) else []
    return v


# ---------------------------------------------------------------- 隔离

def mask_foreign(text, other_answers, other_labels):
    """意见发给某个生产者之前：抹掉其他生产者的答案原文和代号。返回 (新文本, 抹了几处)。"""
    n = 0
    for a in other_answers:
        a = (a or "").strip()
        if len(a) >= 3 and a in text:
            n += text.count(a)
            text = text.replace(a, "【他人答案已隐去】")
    for l in other_labels:
        pat = re.compile(r"(?:解题者|生产者|角色)?" + re.escape(l))
        text, k = pat.subn("其他生产者", text)
        n += k
    return text, n


# ---------------------------------------------------------------- 报告

def judge_prompt(problem, a_label, a, b_label, b):
    """生成可复制的提示词：拿去问别的模型，两个答案是否等价。"""
    return (f"下面是一道数学题和两个答案。请判断这两个答案在数学上是否等价（表达形式不同但值相同算等价）。\n"
            f"先简要说明理由，最后一行只写「等价」或「不等价」或「无法判断」。\n\n题目：\n{problem}\n\n"
            f"答案一（{a_label}）：{a}\n答案二（{b_label}）：{b}\n")


def _latest(project, etype):
    out = {}
    for r in project.q("SELECT ref_id, content, seq FROM events WHERE type=? ORDER BY seq", (etype,)):
        out[r["ref_id"]] = json.loads(r["content"])
    return out


def build_report(project):
    from ..report import write
    cfg = project.config
    items = cfg["items"]
    producers = []
    for r in project.q("SELECT content FROM events WHERE type='提交' ORDER BY seq"):
        c = json.loads(r["content"])
        if c["role"] not in producers:
            producers.append(c["role"])
    finals = _latest(project, "最终状态")
    cmp_ = _latest(project, "对照标准答案")
    human = _latest(project, "人工判断")
    setting = _latest(project, "代码运行设置")

    def verdict(key):
        h = human.get(key)
        if h:
            return {"对": "对", "错": "错"}.get(h["verdict"], "待人工判断"), "人工判断"
        c = cmp_.get(key)
        if not c:
            return None, ""
        how = c.get("判定方式")
        label = "自动比对" + (f"：{how}" if how and how != "文字一致" else "")
        if how == "小数近似≠精确值":
            label = "自动比对：近似小数不等价，表示层错误（若题目明确要求给近似值则不算错，请人工复核）"
        if c["result"] in ("未得出", "部分得出"):
            return c["result"], "生产者/各方自己声明"
        if "仅一方得出" in (c.get("判定方式") or "") and c["result"] in ("自动一致", "不一致"):
            return "仅一方得出", f"没有独立的第二方；它的答案与标准答案{'一致' if c['result'] == '自动一致' else '不一致'}，仅供参考"
        return {"自动一致": "对", "不一致": "错"}.get(c["result"], "待人工判断"), label

    rows, tally = [], {}
    for it in items:
        iid = it["id"]
        f = finals.get(iid, {})
        row = {"条目": iid, "题目": it.get("name") or it["problem"][:30], "标准答案": it.get("gold"),
               "状态": f.get("状态", "未完成"), "原因": f.get("原因"), "一致方式": f.get("一致方式"),
               "共识答案": f.get("共识"), "轮数": f.get("轮数"),
               "各生产者首轮答案": f.get("首轮", {}), "各生产者最终答案": f.get("最终", {}),
               "验证概况": f.get("验证概况", []), "未实测或不确定项": f.get("未实测项", []),
               "独立性提示": f.get("独立性提示", []), "未声明的改答案": f.get("未声明的改答案", []),
               "未处理的次要意见": f.get("未处理次要意见", []), "争议详情": f.get("争议详情"),
               "未得出或部分得出者": f.get("未得出或部分得出者", {}), "格式警告": f.get("格式警告", []),
               "未验证的部分": (f["未验证的部分"] if "未验证的部分" in f else
                                [{"来源": "（旧版本记录：未实测项）", "对象": "", "内容": x, "试过": ""} for x in f.get("未实测项", [])]),
               "作者验证未通过却提交": f.get("作者验证未通过却提交", [])}
        flagged = bool([x for x in row["未验证的部分"] if re.fullmatch(r"审查者[^ ]{1,16}", str(x.get("来源", "")))
                        and "验证步骤" not in str(x.get("来源", ""))]
                       or row["作者验证未通过却提交"])  # 只数审查者自己列的"未验证的部分"，程序自动补的不算
        for key in [f"{iid}|基线:{r}" for r in producers] + [f"{iid}|最终"]:
            v, how = verdict(key)
            row["判定·" + key.split("|", 1)[1]] = f"{v}（{how}）" if v else None
            cand = (cmp_.get(key) or {}).get("候选值与标准答案")
            tally.setdefault(key.split("|", 1)[1], []).append((v, cand, flagged and key.endswith("|最终")))
        if row["作者验证未通过却提交"]:
            row["提示"] = "作者自己的验证没有通过却提交了答案"
        rows.append(row)

    def acc(tv):
        vals = [x[0] for x in tv]
        n, ok = len(vals), sum(v == "对" for v in vals)
        wrong, pend = sum(v == "错" for v in vals), sum(v == "待人工判断" for v in vals)
        none_, part = sum(v == "未得出" for v in vals), sum(v == "部分得出" for v in vals)
        one = sum(v == "仅一方得出" for v in vals)
        rest = n - ok - wrong - pend - none_ - part - one
        decided = ok + wrong
        part_hit = sum(1 for v, c, _ in tv if v == "部分得出" and c == "自动一致")
        none_hit = sum(1 for v, c, _ in tv if v == "未得出" and c == "自动一致")
        part_cand = sum(1 for v, c, _ in tv if v == "部分得出" and c)
        ok_flag = sum(1 for v, c, f in tv if v == "对" and f)
        return {"题数": n, "对": ok, "错": wrong, "未得出": none_, "部分得出": part, "仅一方得出": one,
                "无共识或未比对": rest, "待人工判断": pend,
                "正确率区间": f"{ok}/{n} ~ {ok + pend}/{n}" if pend else f"{ok}/{n}",
                "已判定的题里对多少": f"{ok}/{decided}" if decided else "—",
                "选择不答的比例": f"{none_ + part}/{n}",
                "部分得出中有候选值的": part_cand,
                "部分得出中候选值与标准答案一致的": part_hit,
                "未得出中候选值与标准答案一致的": none_hit,
                "对且带未验证部分或作者验证未通过的": ok_flag}
    stats = {k: acc(v) for k, v in tally.items()}
    extra = _stage27_sections(project, items, producers, finals, verdict)
    ok, bad, n = project.verify_chain()
    # 2.6 修：按账本里实际的「代码运行」记录判断，不只看设置（设置可能中途改过）
    code_runs = project.q("SELECT COUNT(*) c FROM events WHERE type='代码运行'")[0]["c"]
    code_on = code_runs > 0 or any(s.get("允许运行代码") for s in setting.values())
    code_changed = len({bool(s.get("允许运行代码")) for s in setting.values()}) > 1
    done = bool(project.q("SELECT 1 FROM events WHERE type='迭代结束'"))
    if done:
        stop_reason = None
    else:
        last = project.q("SELECT type FROM events WHERE type IN ('用户停止运行','调用失败暂停','迭代结束') "
                         "ORDER BY seq DESC LIMIT 1")
        stop_reason = {"用户停止运行": "用户终止（已完成的调用都保留，重新运行会从停下处继续，完成后本结论会被新结论替换）",
                       "调用失败暂停": "调用失败暂停（检查问题后重新运行即可继续）"}.get(last[0]["type"] if last else "",
                                                                    "尚未跑完")
    notes = [
        "三态：生产者可以如实写「得出 / 部分得出 / 未得出」。不知道是合法答案，不会被格式校验打回；"
        "「未得出」「部分得出」不计为错，也不计为对，单独列出，并给出「已判定的题里对多少」和「选择不答的比例」，"
        "用来看模型是在乱答，还是知道自己的边界。审查者会检查「未得出」的理由是否成立（是否真的试过、卡点是否真的卡住）。",
        "状态说明：闭合＝至少两位生产者得出且一致（只有一位生产者时一位即可），另有人未完整得出的不算反对，会注明；"
        "未得出／部分得出＝各方都没有得出完整答案且审查者认可理由；仅一方得出＝多位生产者里只有一位得出，没有独立的第二方，不计为对；"
        "有争议＝各方有分歧、意见没处理完、停滞或到轮数上限，由用户裁决。",
        "基线：各生产者在看不到任何人意见的情况下独立作答的第 1 轮答案，用来对照评审流程是否真的有帮助。",
        "最终：评审流程闭合（所有意见关闭且各答案真的一致）后的共识答案；未闭合的题标为有争议或仅一方得出，不计为对，由用户裁决。",
        "审查者不做题，只比对并独立验证；意见发给生产者时互相隔离，不含其他生产者的答案。不按人数投票。",
        "「程序验证过的」＝程序比对答案、程序运行代码得到的输出、与标准答案的比对；「模型判断的」＝审查者的层级评价、"
        "对验证结果的诊断、对答案是否等价的判断。两者在报告里分开列。",
        "与标准答案的比对：先规范化（含 Unicode 符号转 LaTeX），再按 60 位精度做数值验证（含变量的表达式在多个随机点代入、"
        "区间与元组按结构比较，括号开闭必须一致）；程序解析不了或拿不准的列入待人工判断，由用户判定后记入账本（来源标为人工判断）。"
        "小数近似与精确闭式（如 1.414 与 \\sqrt{2}）判为不一致，记为表示层错误；例外：标准答案本身是小数时，按标准答案的精度比；"
        "有限小数与其分数（0.5 与 1/2）算一致。标准答案只在流程全部结束后才由程序比对，从不出现在任何提示词中。",
        (("本次允许运行模型写的代码" + ("（运行中途改过这项设置，见账本「代码运行设置」）" if code_changed else "")
          + f"，实际运行了 {code_runs} 段代码；运行器为 A 档，不是安全隔离（见使用说明）。") if code_on
         else "本次没有运行任何代码（参数面板未勾选「允许运行模型写的代码」），所有题的验证结论为「未验证」。"),
    ]
    conclusion = {"问题": cfg["question"], "口径": cfg["scope"],
                  "项目状态": "已完成" if done else "未完成", "逐题结果": rows,
                  "正确率（基线=各生产者首轮独立作答；最终=评审闭合后的共识）": stats, "说明": notes,
                  "账本完整性": {"完整": ok, "第一个不一致事件": bad, "事件数": n, "说明": "事件数不含随后记入的「输出结论」事件"}}
    conclusion.update(extra["data"])
    if not done:  # 规格 10.3：项目被终止或没跑完时同样出结论：结论＝未得出，另写终止原因
        conclusion["结论"] = "未得出"
        conclusion["终止原因"] = stop_reason
    md = [f"# 结论：{cfg['question']}", "", f"口径：{cfg['scope']}", ""]
    if not done:
        md += [f"项目状态：未完成。结论：未得出。终止原因：{stop_reason}", ""]
    md += ["## 正确率", "",
           "| 对象 | 题数 | 对 | 错 | 未得出 | 部分得出 | 仅一方得出 | 待人工判断 | 无共识 | 正确率 | 已判定的题里对多少 | 选择不答的比例 |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    md += [f"| {k} | {v['题数']} | {v['对']} | {v['错']} | "
           f"{v['未得出']}（其中候选值与标准答案一致 {v['未得出中候选值与标准答案一致的']}） | "
           f"{v['部分得出']}（其中候选值与标准答案一致 {v['部分得出中候选值与标准答案一致的']}） | {v['仅一方得出']} | {v['待人工判断']} | "
           f"{v['无共识或未比对']} | {v['正确率区间']} | {v['已判定的题里对多少']} | {v['选择不答的比例']} |"
           for k, v in stats.items()]
    last = stats.get("最终")
    if last:
        md += ["", f"最终一列里，「对」{last['对']} 题中有 {last['对且带未验证部分或作者验证未通过的']} 题带有未验证的部分或作者验证未通过"
                   "（值与标准答案一致，但依据没有全部核对，见各题的「未验证的部分」；这是提示，不是扣分）。",
               "「未得出／部分得出」不算对也不算错，但在需要“两头都控制在 1%”的评测里，它们算漏报（该答而没答）；"
               "括号里的「候选值与标准答案一致」是：这些题里模型给出的候选值其实与标准答案相同，用来区分“真不会”和“会但不敢认”。"
               "漏报的原因（口径比题目要求更严，还是证据确实不足）需要人看账本判断，程序不自动归类。",
               "注意：生产者写“未得出”时程序会清空它的 answer（不让编的东西混进去），所以“未得出”那栏的候选值子计数基本是 0，"
               "有数的主要是“部分得出”。"]
    md += extra["md_top"]
    tm = role_timing(project)
    if tm:
        md += ["", "## 各角色用时", "",
               "| 角色 | 模型 | 调用次数 | 平均每次用时（秒） | 平均首包等待（秒） | 输出速度（tokens/秒） | 最长一次无数据间隔（秒） |",
               "|---|---|---|---|---|---|---|"]
        dash = lambda x: "—" if x is None else x  # noqa: E731
        md += [f"| {t['角色']} | {t['模型']} | {t['调用次数']} | {dash(t['平均用时秒'])} | {dash(t['平均首包秒'])} | "
               f"{dash(t['输出速度'])} | {dash(t['最长无数据间隔秒'])} |" for t in tm]
        md += ["", "输出速度＝该角色全部调用的输出 token 总数 ÷ 总用时（含思考；用时含首包等待与重试前的失败调用）。"]
        conclusion["各角色用时"] = tm
    md += ["", "## 逐题", "", "| 条目 | 状态 | 一致方式 | 共识答案 | 标准答案 | 轮数 | 判定 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        js = "；".join(f"{k[3:]}:{v}" for k, v in r.items() if k.startswith("判定·") and v)
        md.append(f"| {r['条目']} | {r['状态']} | {r['一致方式'] or ''} | {r['共识答案'] or ''} | {r['标准答案']} | "
                  f"{r['轮数'] or ''} | {js} |")
    for r in rows:
        md += ["", f"## {r['条目']}　{r['状态']}" + (f"（{r['原因']}）" if r["原因"] else "")]
        md.append("- 首轮各生产者答案：" + "；".join(f"{k}：{v}" for k, v in r["各生产者首轮答案"].items())
                  + "　最终：" + "；".join(f"{k}：{v}" for k, v in r["各生产者最终答案"].items()))
        if r["验证概况"]:
            md.append("- 程序验证过的：" + "；".join(
                f"{v['生产者']}：作者代码{v['作者代码']}、独立实现{v['独立实现']}" for v in r["验证概况"]))
            md.append("- 模型判断的：" + "；".join(
                f"{v['生产者']}：{v['结论']}" + (f"（诊断：{v['诊断']}）" if v.get("诊断") and v["诊断"] != "无" else "")
                for v in r["验证概况"]))
        if r.get("提示"):
            md.append("- ⚠ " + r["提示"] + "：" + "；".join(
                f"{x['生产者']}（第 {x['轮']} 轮，验证代码{x['状态']}）{x['摘要']}" for x in r["作者验证未通过却提交"]))
        if r["验证概况"]:
            md.append("- 核对类型：" + "；".join(
                f"{v['生产者']}：" + (
                    "（旧版本未记录核对类型）" if "核对类型" not in v else
                    "没有做独立实现" if v.get("核对类型", "无") == "无" else
                    ("仅同路线复现，只核对了实现，未核对模型假设" if v["核对类型"] == "同路线复现" else
                     ("另一方法核对" + (f"（{v['核对说明']}）" if v.get("核对说明") else "") if v["核对类型"] == "另一方法" else
                      v["核对类型"] + (f"（{v['核对说明']}）" if v.get("核对说明") else ""))))
                for v in r["验证概况"]))
        if r["未验证的部分"]:
            md.append(f"- 未验证的部分（{len(r['未验证的部分'])} 项；不挡闭合，但引用本题结论时必须一并引用）：")
            for x in r["未验证的部分"]:
                md.append(f"  - [{x['来源']}{'·' + x['对象'] if x.get('对象') else ''}] {x['内容']}"
                          + (f"　试过：{x['试过']}" if x.get("试过") else ""))
        else:
            md.append("- 未验证的部分：审查者和生产者都没有列出。")
        for k, lab in (("独立性提示", "独立性"), ("未声明的改答案", "改了答案但没有声明改动的生产者"),
                       ("未处理的次要意见", "未处理的次要意见")):
            if r[k]:
                md.append(f"- {lab}：" + "；".join(str(x) for x in r[k]))
        if r["未得出或部分得出者"]:
            md.append("- 未完整得出的生产者：" + "；".join(
                f"{k}（{v['状态']}）已确定：{v['已确定'] or '无'}；未解决：{v['未解决'] or '无'}"
                for k, v in r["未得出或部分得出者"].items()))
        if r["格式警告"]:
            md.append("- 格式警告（程序已整理，供你知晓）：" + "；".join(r["格式警告"]))
        if r["争议详情"]:
            md.append("- 争议详情（请你裁决）：")
            for d in r["争议详情"]:
                md.append(f"  - {d['生产者']}：答案 {d['答案']}；最后一轮收到的意见：{d['收到的意见'] or '无'}；"
                          f"它的回应：{d['回应'] or '无'}")
    md += extra["md_items"]
    pend = pending(project)
    if pend:
        md += ["", "## 待人工判断", "", "下面这些题，程序无法确定两个答案是否等价。可以把提示词复制去问别的模型，再自己做判断。", ""]
        for p in pend:
            md += [f"## {p['item']} · {p['target']}", "", "```", p["prompt"], "```", ""]
    md += ["", "说明："] + [f"- {x}" for x in notes]
    md += ["", f"账本完整性：{'完整' if ok else '不完整，第一个不一致事件 ' + str(bad)}"
               f"（共 {n} 条事件，不含随后记入的「输出结论」这一条；账本里会比这个数多 1 条）"]
    return write(project, conclusion, md)


def _stage27_sections(project, items, producers, finals, verdict):
    """2.7：流程价值的计数、冻结的表单（放行方式、用户改动）、错误归因（人工标注）、找茬者、裸题对照。"""
    from ..fields.answer import compare_detail
    from .. import formcheck as FC
    data, top, per = {}, [], []
    fro, cands, attr, bare = {}, {}, {}, {}
    for r in project.q("SELECT type, ref_id, content FROM events WHERE type IN ('表单冻结','表单候选','错误归因','裸题对照') ORDER BY seq"):
        c = json.loads(r["content"])
        {"表单冻结": fro, "表单候选": cands, "错误归因": attr, "裸题对照": bare}[r["type"]][r["ref_id"]] = c
    gold_items = [it for it in items if it.get("gold") not in (None, "")]
    if gold_items and finals:
        any_first = both = fix = broke = closed = passed_wrong = 0
        for it in gold_items:
            iid = it["id"]
            base = [verdict(f"{iid}|基线:{r}")[0] for r in producers]
            fin = verdict(f"{iid}|最终")[0]
            af = any(v == "对" for v in base)
            any_first += af
            fix += (not af) and fin == "对"
            broke += af and fin != "对"
            if (finals.get(iid) or {}).get("状态") == "闭合":
                closed += 1
                passed_wrong += fin == "错"
        n = len(gold_items)
        cnt = {"有标准答案的题数": n, "首轮至少一方答对": any_first, "首轮全错、最终答对": fix,
               "首轮有人对、最终却错": broke, "闭合的题数": closed, "闭合了却是错的（审查者放过错误）": passed_wrong}
        data["流程价值计数"] = cnt
        top += ["", "## 流程价值的几个计数", "",
                "| 有标准答案的题数 | 首轮至少一方答对 | 首轮全错、最终答对 | 首轮有人对、最终却错 | 闭合的题数 | 闭合了却是错的（审查者放过错误） |",
                "|---|---|---|---|---|---|",
                f"| {n} | {any_first}/{n} | {fix} | {broke} | {closed} | {passed_wrong}/{closed if closed else 0} |", "",
                "「首轮至少一方答对」是基线，不是上限：流程是迭代的，审查者的反驳发回生产者后，首轮全错的题也可能最终答对"
                "（这是流程价值最有力的证据）；反过来「首轮有人对、最终却错」说明流程把对的答案带偏了。"
                "「闭合了却是错的」＝各方一致且审查者都放行，但与标准答案不符。"]
    ffn = project.q("SELECT COUNT(*) c FROM events WHERE type='找茬'")[0]["c"]
    if ffn:
        issues = sum(len(json.loads(r["content"]).get("问题") or []) for r in project.q("SELECT content FROM events WHERE type='找茬'"))
        adopted = project.q("SELECT COUNT(*) c FROM events WHERE type IN ('评审意见','表单意见') AND content LIKE '%找茬者问题（审查者复查后采纳）%'")[0]["c"]
        data["找茬者"] = {"出场次数": ffn, "提出的问题": issues, "被审查者采纳": adopted}
        top += ["", f"找茬者：出场 {ffn} 次，提出问题 {issues} 条，审查者复查后采纳 {adopted} 条（找茬者不挡闭合，意见只交审查者）。"]
    if bare:
        rows = {}
        for key, c in bare.items():
            it = next((x for x in items if x["id"] == c.get("item")), None)
            g = (it or {}).get("gold")
            res = ""
            if c.get("answer") and g not in (None, ""):
                res = {"自动一致": "对", "不一致": "错"}.get(compare_detail(c["answer"], g, None, None, b_is_gold=True)["result"], "待人工判断")
            rows[key] = {"答案": c.get("answer") or "", "与标准答案": res or ("—" if not c.get("answer") else "无标准答案"),
                         "说明": c.get("说明") or ""}
        data["裸题对照"] = rows
        top += ["", "## 裸题对照（只给题目本身，不加任何角色说明和格式要求）", "",
                "| 题·生产者 | 答案 | 与标准答案 | 说明 |", "|---|---|---|---|"]
        top += [f"| {k} | {v['答案']} | {v['与标准答案']} | {v['说明']} |" for k, v in sorted(rows.items())]
        top += ["", "唯一的附加是「最后一行写最终答案」，否则程序取不出答案来比对。裸题答对、流程里却答错，说明我们的包装拖了后腿；"
                    "裸题也错，是模型本身的能力问题。测的是商业 API 版本，不和榜单数字直接比较。"]
    forms = {}
    for it in items:
        iid = it["id"]
        f, c = fro.get(iid), cands.get(iid)
        if not (f or c):
            continue
        info = {"表单阶段": (c or {}).get("状态"), "表单阶段轮数": (c or {}).get("轮数"),
                "放行方式": (f or {}).get("来源") or "未冻结", "用户改动": len((f or {}).get("用户改动") or []),
                "冻结版": (f or {}).get("冻结版"), "错误归因（人工）": attr.get(iid)}
        forms[iid] = info
        per += ["", f"## {iid}　冻结的表单",
                f"- 表单阶段：{info['表单阶段'] or '—'}（{info['表单阶段轮数'] or '—'} 轮）；放行：{info['放行方式']}；用户改动 {info['用户改动']} 处"]
        if (c or {}).get("原因"):
            per.append(f"- 表单阶段未一致的原因：{c['原因']}")
        if f:
            per += ["", "```", FC.render(f["冻结版"]), "```"]
            for d in f.get("用户改动") or []:
                per.append(f"- 用户改动·{d['位置']}：{json.dumps(d['起草'], ensure_ascii=False)} → {json.dumps(d['冻结'], ensure_ascii=False)}")
        a = attr.get(iid)
        per.append(f"- 错误归因（人工标注）：{a['归因']}" + (f"（{a['note']}）" if a.get("note") else "") if a else
                   "- 错误归因（人工标注）：未标注。结果不对时，请判断错在表单（题意拆错了）还是做题；判断不了就留着，交给能判断的人看。")
    if forms:
        data["表单"] = forms
    return {"data": data, "md_top": top, "md_items": per}


def role_timing(project):
    """2.5：各角色的平均用时、首包等待、输出速度（tokens/秒）、最长无数据间隔——全部从账本 calls 表算。"""
    out = []
    for r in project.q("SELECT role, provider, model, COUNT(*) n, AVG(duration) d, AVG(first_byte) fb, "
                       "SUM(CASE WHEN duration>0 THEN COALESCE(output_tokens,0) END) o, SUM(duration) sd, "
                       "MAX(max_gap) mg FROM calls WHERE duration IS NOT NULL GROUP BY role ORDER BY role"):
        out.append({"角色": r["role"], "模型": r["model"], "调用次数": r["n"],
                    "平均用时秒": round(r["d"], 1) if r["d"] is not None else None,
                    "平均首包秒": round(r["fb"], 1) if r["fb"] is not None else None,
                    "输出速度": round(r["o"] / r["sd"], 1) if r["sd"] and r["o"] else None,
                    "最长无数据间隔秒": round(r["mg"], 1) if r["mg"] is not None else None})
    return out


def pending(project):
    """还没有人工判断、程序也判不了的比对项（含可复制的提示词）。"""
    cmp_ = _latest(project, "对照标准答案")
    human = _latest(project, "人工判断")
    by_id = {it["id"]: it for it in project.config["items"]}
    out = []
    for key, c in cmp_.items():
        if c["result"] == "待人工判断" and key not in human:
            iid, target = key.split("|", 1)
            it = by_id[iid]
            out.append({"key": key, "item": iid, "target": target, "answer": c["answer"], "gold": c["gold"],
                        "prompt": judge_prompt(it["problem"], "模型答案", c["answer"], "标准答案", c["gold"])})
    return out
