"""题目包：近年诺贝尔奖各奖项的得主与获奖原因（1.8，第二道整理类题）。

问题文件里写 "task": "nobel"。每个条目是"某年某奖"，生产者填两项：
- 得主：原文（西文）名字，多个用"；"隔开；程序核对每个名字都在所引原文里，比对时忽略大小写、变音符号、中间名缩写；
- 获奖原因：官方表述原文（通常以 for 开头的那句），程序逐字核对；
另可填"中文参考"（模型翻译，仅供参考，程序不核对）。
"查到"的规则与事实汇编相同：两方一致＋原句在原文里＋另一可信网站独立支持。
"""

from __future__ import annotations

import re
from collections import Counter

from .. import report as R
from ..fields.record import RecordField, split_names
from ..util import canonical_json
from .fact_compilation import (ENTRY_STATES, aliases, find_windows, review_verdict,  # noqa: F401
                               validate_review)
from .fact_compilation import excerpts_for as _excerpts_for

BLOCK_SCHEMA = {"条目": ["奖项", "状态"]}
FIELD = RecordField(noun="获奖")

AWARD_RE = re.compile(r"awarded|laureate|prize motivation|\bfor\b|获奖|授予|颁给|颁发给|得主|表彰", re.I)
YEAR_NEAR = re.compile(r"(?<!\d)20[12]\d(?!\d)")

SYSTEM_PRODUCER = """你是多模型互查流程中的"生产者"。任务是资料整理：为每个条目（某年某个诺贝尔奖）查出得主和获奖原因，并给出出处。
规则（必须遵守）：
1. 只能引用程序提供给你的检索原文（你自己检索到的）；出处填检索编号（形如 S00012），原句必须从该检索原文中逐字摘抄（空格、标点可以不同，文字须一致）。原句最好是写明"某年某奖授予谁、因为什么"的那一句。
2. 得主：照原文写原名（西文写法，如 Anne L'Huillier），不要只写中文译名；有几位写几位，用"；"隔开；得主是机构的写机构的原文名称。程序会核对每个名字都在原文里。
3. 获奖原因：从原文逐字引用官方表述（诺贝尔官网上通常是以 for 开头的那一句），不要改写、不要翻译。程序会核对它在原文里。一个奖分几部分颁发、各部分有各自的获奖原因的（例如"一半授予甲 for …，另一半授予乙和丙 for …"），把几句分别照抄，用"；"隔开，不要拼成一句。
4. 中文参考：可以把获奖原因译成中文写在这里，仅供阅读，程序不核对。
5. 查不到就如实填"资料缺载"，不得凭记忆编造；如果只能凭记忆给出，出处填"模型记忆"（会被标为未查证）。
6. 不同来源说法不一致时，状态填"有争议"，并在 fields.异说 中列出另一说法及其检索编号。
7. 只输出一个 JSON 对象，不要输出其他文字。不要在说明文字中声称"已验证""已核对"。
8. changes 里的 type 只能是"数值""判据""结论""措辞"这四个词之一，不要填字段名。"""

SYSTEM_REVIEWER = """你是多模型互查流程中的"审查者"。你逐条核对生产者提交的条目块，做两项独立的判断：
一、等价核对：生产者填的得主名单和获奖原因，与原句及其前后文所说是否一致（是不是这一年、这一个奖；得主有没有多写、漏写；获奖原因是不是这一奖的官方表述）。名字的大小写、变音符号、中间名缩写不同不算不一致。
二、独立核验：程序另外附上了生产者没有引用的可信网站原文段落（独立来源）。判断独立来源是否支持该得主名单和获奖原因：支持 / 矛盾 / 无独立来源（没有附独立来源，或附的段落里没有讲到这一年这一奖的得主）。独立来源是中文、只写了中文译名的，名单对得上也算支持。
只依据程序给你的文字判断，不要凭记忆。只输出一个 JSON 对象，不要输出其他文字。"""


def windows(text, keys, width=420, top=2):
    """原文里"某年某奖"附近讲到授奖的片段（忽略大小写）。"""
    return find_windows(text, keys, AWARD_RE, YEAR_NEAR, width, top, fold=True)


def excerpts_for(project, retrieval_ids, item, max_sources=4, trusted=()):
    return _excerpts_for(project, retrieval_ids, item, max_sources, trusted, windows_fn=windows)


def independent_query(item):
    """程序替审查者在可信网站内检索时用的检索词（不含任何得主名字）。"""
    return item.get("query") or f"{item['name']} 得主"


def same_category_note(x):
    return ""


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
              "检索词写年份和奖项名称即可（中英文都可以，例如 Nobel Prize in Physics 2023），不要写得主名字，以免只搜到附和某个名字的网页。",
              '输出格式：{"searches": [{"item": "N01", "query": "检索词"}]}']
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
        '{"blocks": [{"type": "条目", "item": "N01", "fields": {"奖项": "某年某奖", '
        '"得主": "照原文写原名，多位用；隔开；查不到填 资料缺载", "获奖原因": "从原文逐字引用的官方表述",'
        ' "中文参考": "获奖原因的中文翻译（仅供参考）", "状态": "查到/有争议/资料缺载", "出处": "S00012 或 模型记忆",'
        ' "原句": "从原文逐字摘抄", "异说": "（有争议时填写）"}}],',
        ' "notes": "简短说明，不含需检查的主张",',
        ' "changes": [{"type": "只能是 数值、判据、结论、措辞 之一", "item": "N01", "说明": "改了什么"}],',
        ' "responses": [{"opinion": "O00001", "action": "采纳/驳回", "evidence": "S00012", "reason": "…"}]}',
        "第 1 轮 changes 与 responses 可以为空数组。changes 的 type 例：改了得主或获奖原因填\"数值\"，改了原句或出处填\"措辞\"。",
    ]
    return "\n".join(lines)


def review_prompt(config, author_role, blocks_view):
    lines = [f"口径：{config['scope']}", f"被审产物作者：{author_role}", "",
             "逐条核对以下条目块。每条附有：原句及其前后文、所填得主名单与获奖原因、程序规整后的写法、"
             "程序另找的独立来源段落（生产者没有引用的可信网站）。", ""]
    for b in blocks_view:
        lines.append(canonical_json(b))
    lines += ["", '输出格式：{"reviews": [{"block": "块编号", "equivalent": "等价/不等价/无法判断",'
              ' "independent": "支持/矛盾/无独立来源", "independent_source": "所依据的独立来源编号（没有则空）",'
              ' "reason": "理由", "suggestion": "建议（可空）"}]}']
    return "\n".join(lines)


# ---------------- 结论 ----------------
def _chinese_ref(project, iid):
    for b in project.q("SELECT fields FROM blocks WHERE item_id=? AND st_valid='有效' ORDER BY round DESC, block_id",
                       (iid,)):
        import json
        v = json.loads(b["fields"]).get("中文参考")
        if v:
            return v
    return None


def build_report(project):
    items = [dict(r) for r in project.q("SELECT * FROM items ORDER BY item_id")]
    meta = {x["id"]: x for x in project.config.get("items", [])}
    rows = []
    for it in items:
        m = meta.get(it["item_id"], {})
        state = R.state_of(it)
        names, cit = FIELD._parts(it["value"]) if it["value"] and FIELD.SEP in (it["value"] or "") else (None, None)
        rows.append({"条目": it["item_id"], "分组": m.get("group"), "年份": m.get("year"), "奖项": it["name"],
                     "结论": state, "得主": names, "得主人数": len(split_names(names)) if names else None,
                     "获奖原因（官方原文）": cit, "中文参考（模型翻译，未核对）": _chinese_ref(project, it["item_id"]),
                     "查证": it["st_check"], "验证": it["st_verify"], "独立性": it["st_indep"],
                     "独立验证次数": it["indep_count"], "备注": it["note"]})
    ok, bad, n = project.verify_chain()
    groups = list(dict.fromkeys(r["分组"] for r in rows))
    stats = []
    for g in groups:
        rs = [r for r in rows if r["分组"] == g]
        c = Counter(r["结论"] for r in rs)
        stats.append({"奖项": g, "条目数": len(rs), "查到": c.get("查到", 0),
                      "查到条目的得主人数合计": sum(r["得主人数"] or 0 for r in rs if r["结论"] == "查到")})
    conclusion = {
        "问题": project.config["question"], "口径": project.config["scope"], "逐条结果": rows,
        "统计（由代码计算）": stats,
        "说明": ["\"查到\"须同时满足：两个生产者各自查到且一致（得主名单相同、获奖原因相同）、审查者判定与原句一致、"
               "审查者用另一可信网站（生产者引用的网站以外）上的原文独立核验并支持。",
               "得主名字由程序核对确实出现在所引原文里；比对时忽略大小写、变音符号、中间名缩写。获奖原因由程序核对是原文的逐字引用。",
               "\"中文参考\"是模型翻译，仅供阅读，程序没有核对，不作为结论的一部分。",
               "经济学奖的正式名称是\"瑞典中央银行纪念阿尔弗雷德·诺贝尔经济学奖\"，严格说不属于诺贝尔奖，按惯例一并列出。",
               "\"待复核\"：两方一致，但没有另一可信网站的独立来源支持；\"仅一方查到\"：只有一方通过查证；"
               "\"有争议\"：两方不一致，或独立来源与两方结果矛盾（不按多数定）。",
               "第一期未实现找茬者与准入门；结论不含找茬环节。"],
        "账本完整性": {"完整": ok, "第一个不一致事件": bad, "事件数": n},
    }
    md = [f"# 结论：{conclusion['问题']}", "", f"口径：{conclusion['口径']}"]
    for g in groups:
        md += ["", f"## {g}", "", "| 年份 | 结论 | 得主 | 获奖原因（官方原文） | 中文参考（模型翻译，未核对） | 独立验证次数 | 说明 |",
               "|---|---|---|---|---|---|---|"]
        for r in rows:
            if r["分组"] == g:
                md.append(f"| {r['年份'] or ''} | {r['结论']} | {r['得主'] or ''} | {r['获奖原因（官方原文）'] or ''} | "
                          f"{r['中文参考（模型翻译，未核对）'] or ''} | {r['独立验证次数']} | {r['备注'] or ''} |")
    md += ["", "## 统计（由代码计算）", "", "| 奖项 | 条目数 | 查到 | 查到条目的得主人数合计 |", "|---|---|---|---|"]
    md += [f"| {s['奖项']} | {s['条目数']} | {s['查到']} | {s['查到条目的得主人数合计']} |" for s in stats]
    md += ["", "说明："] + [f"- {x}" for x in conclusion["说明"]]
    md += ["", f"账本完整性：{'完整' if ok else '不完整，第一个不一致事件 ' + str(bad)}（共 {n} 条事件）"]
    return R.write(project, conclusion, md)
