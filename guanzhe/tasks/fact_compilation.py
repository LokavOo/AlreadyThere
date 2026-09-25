"""事实汇编规则包（11.3）：以"历代皇帝出生日期 → 生肖分组统计"为试跑任务。

口径由用户在项目配置中给定（条目清单 + 口径说明）。
每个条目由各生产者独立检索、独立撰写一个"条目块"；生肖归类、计数、占比全部由代码完成。
"""

from __future__ import annotations

import re
from collections import Counter

from ..fields import DateField, format_date, normalize_date  # noqa: F401（1.7：日期处理移到字段核对器）
from ..util import canonical_json

ZODIAC = "鼠牛虎兔龙蛇马羊猴鸡狗猪"

BLOCK_SCHEMA = {
    # 条目块：状态为 查到/有争议 时须附出处与原句，并至少填出生日期或农历原文之一
    "条目": ["人物", "状态"],
}

ENTRY_STATES = ("查到", "有争议", "资料缺载")

# 1.7：本题目包的主字段及其类型。主流程按这里声明的核对器处理，不再直接处理日期。
FIELD = DateField(key="出生日期", noun="出生", marks=r"生|诞|誕")

SYSTEM_PRODUCER = """你是多模型互查流程中的"生产者"。任务是事实汇编：为每个条目查出人物的出生日期，并给出出处。
规则（必须遵守）：
1. 只能引用程序提供给你的检索原文（你自己检索到的）；出处填检索编号（形如 S00012），原句必须从该检索原文中逐字摘抄（空格、标点可以不同，文字须一致）。
2. 查不到就如实填"资料缺载"，不得凭记忆编造；如果只能凭记忆给出，出处填"模型记忆"（会被标为未查证）。
3. 不同来源说法不一致时，状态填"有争议"，并在 fields.异说 中列出另一说法及其检索编号。
4. 原文用年号纪年的农历日期（如"建文元年二月初九日"）：把这段农历写法原样抄进 fields.农历原文；出生日期可以留空，公历由程序按历表换算，不要自己换算。农历原文只能抄原文里真有的写法，原文里没有的不要填（程序找不到会直接剔除）。原文只用干支纪年、没写年号的（如"己卯岁二月九日"），可以在前面补上你认为对应的年号，写成"建文元年己卯岁二月九日"，干支照原文保留；程序会核对这个年号那一年的干支，对不上就不换算。原文直接写了公历日期的，照原文写进出生日期（YYYY-MM-DD；只有年份的写 YYYY）。
5. 只输出一个 JSON 对象，不要输出其他文字。不要在说明文字中声称"已验证""已核对"。
6. changes 里的 type 只能是"数值""判据""结论""措辞"这四个词之一，不要填字段名。"""

SYSTEM_REVIEWER = """你是多模型互查流程中的"审查者"。你逐条核对生产者提交的条目块，做两项独立的判断：
一、等价核对：生产者从原句转写出的出生日期（以及程序按历表换算的结果）与原句所说是否等价。你不需要自己推算，只比对两者是否说的是同一个日期。
二、独立核验：程序另外附上了生产者没有引用的可信网站原文段落（独立来源）。判断独立来源是否支持该出生日期：支持 / 矛盾 / 无独立来源（没有附独立来源，或附的段落里没有讲到出生日期）。
同一天有几种写法：按惯例的西历（1582 年 10 月 15 日以前用儒略历）、按格里历外推的西历、年号农历、只写年份。程序在"所填日期的等价写法"里列出了换算结果；独立来源用的是其中任何一种写法，都算"支持"，只写了同一年份也算"支持"。换算后仍对不上的才算"矛盾"。
只依据程序给你的文字判断，不要凭记忆。只输出一个 JSON 对象，不要输出其他文字。"""

_ALIAS_RE = re.compile(r"^(明|清|元|宋|唐|汉|漢)(太祖|太宗|世祖|圣祖|聖祖|世宗|高宗|仁宗|宣宗|文宗|穆宗|德宗|成祖|惠帝|英宗|代宗|宪宗|憲宗|孝宗|武宗|神宗|光宗|熹宗|思宗|逊帝|遜帝|景帝)(.+)$")
BIRTH_RE = re.compile(r"生[于於]|出生|诞生|誕生|降生|诞[于於]|誕[於于]|生日|诞辰|誕辰|(?:日|月|年|时|時)\s*生(?![前平活命存])|生\s*[（(]")


def aliases(item):
    """条目的检索用名：去掉括号、拆出年号/庙号/本名，也接受问题文件里给的 aliases。"""
    name = item["name"]
    keys = set(item.get("aliases", []))
    keys.update(re.findall(r"[（(]([^）)]+)[）)]", name))
    base = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
    keys.add(base)
    m = _ALIAS_RE.match(base)
    if m:
        keys.add(m.group(1) + m.group(2))
        keys.add(m.group(3))
    return sorted({k for k in keys if len(k) >= 2}, key=lambda k: (-len(k), k))  # 1.7：同样长的按字排，结果固定


_DATE_NEAR = re.compile(r"\d{3,4}\s*年|[元一二三四五六七八九十廿]+年[^。]{0,8}月")


def birth_windows(text, keys, width=420, top=2):
    """在去排版的原文里找"人名附近讲到出生"的片段。返回 [(得分, 片段)]，得分高的在前。"""
    return find_windows(text, keys, BIRTH_RE, _DATE_NEAR, width, top)


windows = birth_windows  # 1.8：主流程通过 T.windows 调用，各题目包各自定义


def find_windows(text, keys, marks_re, near_re, width=420, top=2, fold=False):
    """通用：在原文里找"条目名附近、有 marks_re 说法和 near_re 内容"的片段。得分 = 3×说法数 + 有内容 2 分
    + 说法与内容相距 40 字以内 5 分。fold=True 时忽略大小写（西文原文用）。"""
    from ..textnorm import t2s
    ts = t2s(text)
    if fold:
        ts = ts.lower() if len(ts.lower()) == len(ts) else ts
        keys = [k.lower() for k in keys]
    cands = []
    for k in keys:
        kk = t2s(k)
        start = 0
        for _ in range(60):
            i = ts.find(kk, start)
            if i < 0:
                break
            start = i + 1
            a, b = max(0, i - width // 3), min(len(text), i + width)
            w = ts[a:b]
            marks = [m.start() for m in marks_re.finditer(w)]  # 只认"出生"一类的说法，不认"民不聊生""生前"
            dates = [m.start() for m in near_re.finditer(w)]
            score = 3 * len(marks) + (2 if dates else 0)
            if marks and dates and min(abs(x - y) for x in marks for y in dates) <= 40:
                score += 5
            cands.append((score, a, b))
    cands.sort(key=lambda x: (-x[0], x[1], x[2]))  # 1.7：得分、起点都相同时再按终点排，结果固定
    out, used = [], []
    for score, a, b in cands:
        if any(not (b <= ua or a >= ub) for ua, ub in used):
            continue
        used.append((a, b))
        out.append((score, text[a:b].strip()))
        if len(out) >= top:
            break
    return out


def excerpts_for(project, retrieval_ids, item, max_sources=4, trusted=(), windows_fn=None):
    """从存档原文（去掉排版符号后）截取"人名附近讲到出生"的段落，按相关程度挑来源（截取规则由程序确定）。"""
    from urllib.parse import urlparse
    from ..textnorm import clean_md
    keys = aliases(item)
    scored, seen = [], set()
    for rid in retrieval_ids:
        row = project.q("SELECT url,title,source,content_hash FROM retrievals WHERE retrieval_id=?", (rid,))[0]
        if row["content_hash"] in seen:  # 同一原文重复检索到，只列一次（保留最早的编号）
            continue
        seen.add(row["content_hash"])
        text = clean_md(project.read_archive(rid) or "")
        wins = (windows_fn or birth_windows)(text, keys)
        best = wins[0][0] if wins else -1
        host = urlparse(row["url"] or "").netloc.lower()
        trust = any(host == t or host.endswith("." + t) or host.endswith(t) for t in trusted)
        scored.append((best, trust, rid, row, wins))
    scored.sort(key=lambda x: (-x[0], not x[1], x[2]))
    out = []
    for best, trust, rid, row, wins in scored[:max_sources]:
        body = "\n……\n".join(w for _, w in wins) if wins else "（原文中未出现该人物名）"
        out.append(f"【{rid}】{row['title']} {row['url']}（{row['source']}）\n{body}")
    return "\n\n".join(out)


def search_prompt(config, items, own_prev=None, opinions=None, others=None):
    lines = [f"口径：{config['scope']}", "", "条目清单："]
    lines += [f"- {it['id']}：{it['name']}" for it in items]
    if own_prev:
        lines += ["", "你之前提交的条目块：", canonical_json(own_prev)]
    if others:
        lines += ["", "其他生产者上一轮的条目块（仅供对照）：", canonical_json(others)]
    if opinions:
        lines += ["", "针对你的未闭合意见："] + [f"- {o['opinion_id']}（条目 {o['item_id']}）：{o['shown']}" for o in opinions]
    lines += ["", "请为需要检索的条目给出检索词（每个条目最多 2 个）。已有可靠出处、且没有针对它的意见的条目可以不检索。",
              "检索词里不要写具体的出生年份或日期（写人名和\"出生\"\"生年\"之类即可），以免只搜到附和某个数字的网页。",
              '输出格式：{"searches": [{"item": "I01", "query": "检索词"}]}']
    return "\n".join(lines)


def write_prompt(config, items, evidence_by_item, round_, own_prev=None, others=None, opinions=None):
    lines = [f"第 {round_} 轮。口径：{config['scope']}", ""]
    for it in items:
        lines.append(f"==== 条目 {it['id']}：{it['name']} ====")
        lines.append(evidence_by_item.get(it["id"]) or "（你还没有这个条目的检索结果）")
        lines.append("")
    if own_prev:
        lines += ["你之前提交的条目块：", canonical_json(own_prev), ""]
    if others:
        lines += ["其他生产者上一轮的条目块（仅供对照；引用只能来自上面你自己的检索原文）：",
                  canonical_json(others), ""]
    if opinions:
        lines += ["针对你的未闭合意见（须逐条在 responses 中处理：采纳或驳回，并附你自己的检索编号作为核验记录）："]
        lines += [f"- {o['opinion_id']}（条目 {o['item_id']}）：{o['shown']}" for o in opinions]
        lines.append("")
    lines += [
        "请为每个条目提交一个条目块。输出格式：",
        '{"blocks": [{"type": "条目", "item": "I01", "fields": {"人物": "…", "出生日期": "原文给的公历 YYYY-MM-DD 或 YYYY；原文只有农历时留空；查不到填 资料缺载",'
        ' "状态": "查到/有争议/资料缺载", "出处": "S00012 或 模型记忆", "原句": "从原文逐字摘抄", "农历原文": "原文中的年号农历写法（如有，原样抄）",'
        ' "异说": "（有争议时填写）"}}],',
        ' "notes": "简短说明，不含需检查的主张",',
        ' "changes": [{"type": "只能是 数值、判据、结论、措辞 之一", "item": "I01", "说明": "改了什么"}],',
        ' "responses": [{"opinion": "O00001", "action": "采纳/驳回", "evidence": "S00012", "reason": "…"}]}',
        "第 1 轮 changes 与 responses 可以为空数组。changes 的 type 例：改了日期填\"数值\"，改了原句或出处填\"措辞\"。",
    ]
    return "\n".join(lines)


def review_prompt(config, author_role, blocks_view):
    lines = [f"口径：{config['scope']}", f"被审产物作者：{author_role}", "",
             "逐条核对以下条目块。每条附有：原句及其前后文、所填出生日期、农历原文与程序按历表换算的结果（如有）、"
             "程序另找的独立来源段落（生产者没有引用的可信网站）。", ""]
    for b in blocks_view:
        lines.append(canonical_json(b))
    lines += ["", '输出格式：{"reviews": [{"block": "块编号", "equivalent": "等价/不等价/无法判断",'
              ' "independent": "支持/矛盾/无独立来源", "independent_source": "所依据的独立来源编号（没有则空）",'
              ' "reason": "理由", "suggestion": "建议（可空）"}]}']
    return "\n".join(lines)


def review_verdict(r):
    """由两项判断推出总判定（兼容只给 verdict 的旧格式）。"""
    eq, ind = r.get("equivalent"), r.get("independent")
    if eq is None and ind is None:
        return r.get("verdict")
    if eq == "不等价" or ind == "矛盾":
        return "错误"
    if eq == "无法判断":
        return "无法判断"
    return "正确"


def validate_review(obj, block_ids):
    from ..util import FormatError
    if not isinstance(obj.get("reviews"), list):
        raise FormatError("缺少 reviews 数组")
    seen = set()
    for r in obj["reviews"]:
        if not isinstance(r, dict):
            raise FormatError("reviews 的每一项须是 JSON 对象")
        if r.get("block") not in block_ids:
            raise FormatError(f"reviews 中的块编号 {r.get('block')!r} 不存在")
        if "equivalent" in r or "independent" in r:
            if r.get("equivalent") not in ("等价", "不等价", "无法判断"):
                raise FormatError(f"equivalent {r.get('equivalent')!r} 不合规，只能是 等价/不等价/无法判断")
            if r.get("independent") not in ("支持", "矛盾", "无独立来源"):
                raise FormatError(f"independent {r.get('independent')!r} 不合规，只能是 支持/矛盾/无独立来源")
        elif r.get("verdict") not in ("正确", "错误", "需补充", "无法判断"):
            raise FormatError(f"verdict {r.get('verdict')!r} 不合规")
        seen.add(r["block"])
    missing = set(block_ids) - seen
    if missing:
        raise FormatError(f"以下块没有给出判定：{sorted(missing)}")


# ---------------- 由代码完成的计算 ----------------

def same_category_note(gz):
    """几种说法落在同一个农历年时的说明（生肖统计用）。"""
    return f"各说法都在农历{gz}年，生肖不受影响"


def zodiac_of(date):
    """由公历出生日期推算生肖。

    春节在公历 1 月 21 日至 2 月 20 日之间；古代日期还存在儒略历/格里历换算差异（约 10 天），
    因此把 1 月 1 日至 3 月 2 日定为"须核对农历"区间，不在此区间内的按公历年份推算。
    只知年份的，无法确定生肖。返回 (生肖或 None, 说明)。
    """
    if not date:
        return None, "无出生日期"
    y, m, d = date
    if m is None:
        return None, "仅知年份，无法确定生肖（须知月日或农历年）"
    if m == 1 or m == 2 or (m == 3 and d <= 2):
        return None, "生于公历 1 月 1 日至 3 月 2 日之间，须核对农历年"
    return ZODIAC[(y - 4) % 12], f"按公历 {y} 年推算（{y} 年春节后出生）"


def zodiac_of_ganzhi(gz):
    if gz and len(gz) >= 2 and gz[1] in "子丑寅卯辰巳午未申酉戌亥":
        return ZODIAC["子丑寅卯辰巳午未申酉戌亥".index(gz[1])], f"按农历{gz}年（程序按历表换算）"
    return None, "无农历年干支"


def tally(results):
    """results: [(条目, 生肖或 None)] -> 统计表。"""
    known = [z for _, z in results if z]
    c = Counter(known)
    n = len(known)
    rows = [{"生肖": z, "人数": c.get(z, 0), "占已确定者比例": (round(c.get(z, 0) / n, 4) if n else None)}
            for z in ZODIAC]
    return {"已确定生肖人数": n, "未能确定人数": len(results) - n, "分组": rows}
