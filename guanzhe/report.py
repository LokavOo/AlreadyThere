"""输出物（第 10 节）第一期：结论 + 全流程记录索引。论文与专家审阅页属第三期。"""

from __future__ import annotations

import json
from pathlib import Path

from . import tasks
from .tasks import fact_compilation as T


def state_of(it):
    """条目的最终结论（各题目包共用）。"""
    note = it["note"] or ""
    if it["st_verify"] == "已验证":
        return "查到"
    if it["value"] == "资料缺载":
        return "资料缺载"
    for k in ("有争议", "待复核", "仅一方查到"):
        if note.startswith(k):
            return k
    return "未得出"


def write(project, conclusion, md):
    out = project.root / "outputs"
    out.mkdir(exist_ok=True)
    (out / "结论.json").write_text(json.dumps(conclusion, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "结论.md").write_text("\n".join(md), encoding="utf-8")
    project.log("程序", "输出结论", content={"file": "outputs/结论.json"})
    return conclusion


def build(project):
    TT = tasks.load(project.config)
    if hasattr(TT, "build_report"):  # 1.8：题目包自带结论格式的，用题目包的
        return TT.build_report(project)
    items = [dict(r) for r in project.q("SELECT * FROM items ORDER BY item_id")]
    group_of = {x["id"]: x.get("group") for x in project.config.get("items", [])}  # 1.2：条目可带分组（如朝代）
    rows, results = [], []
    for it in items:
        note = it["note"] or ""
        state = state_of(it)
        z, why = (None, "不是\"查到\"，不计入生肖统计")
        if state == "有争议" and it.get("ganzhi") and "生肖不受影响" in note:
            z, why = T.zodiac_of_ganzhi(it["ganzhi"])
            why = "日期有争议，但各说法都在同一农历年，生肖不受影响；" + why
        if state == "查到":
            if it.get("ganzhi"):
                z, why = T.zodiac_of_ganzhi(it["ganzhi"])
            else:
                z, why = T.zodiac_of(T.normalize_date(it["value"]))
        results.append((it["item_id"], z))
        rows.append({"条目": it["item_id"], "分组": group_of.get(it["item_id"]), "人物": it["name"], "结论": state, "出生日期": it["value"],
                     "农历年干支": it.get("ganzhi"), "查证": it["st_check"], "验证": it["st_verify"], "独立性": it["st_indep"],
                     "独立验证次数": it["indep_count"], "生肖": z, "生肖说明": why, "备注": it["note"]})
    stats = T.tally(results)
    ok, bad, n = project.verify_chain()
    counts = [r["indep_count"] for r in items if r["st_verify"] == "已验证"]
    conclusion = {
        "问题": project.config["question"],
        "口径": project.config["scope"],
        "逐条结果": rows,
        "生肖统计（由代码计算，计入查到及生肖不受影响的有争议条目）": stats,
        "独立验证次数最小值（已验证条目）": min(counts) if counts else None,
        "说明": ["\"查到\"须同时满足：两个生产者各自查到且一致、审查者判定原句与所填日期等价、"
               "审查者用另一可信网站（生产者引用的网站以外）上的原文独立核验并支持。两方引用同一网站也可以，只要另一可信网站独立支持。",
               "这里的\"查到\"是指从公开的可靠网站能查到并相互印证，用来排除网络谣言，不等于史学考证的定论。",
               "\"待复核\"：两方一致，但没有另一可信网站的独立来源支持；\"仅一方查到\"：只有一方通过查证；"
               "\"有争议\"：两方不一致，或独立来源与两方结果矛盾（不按多数定，因为网站之间常互相转抄）。",
               "写法不同不算矛盾：儒略历与格里历外推、年号农历与公历、只写年份与完整日期，程序换算后是同一天（同一年）的视为一致。",
               "只计入\"查到\"，以及\"有争议\"但各说法都在同一农历年（生肖不受影响，结论中标明）的条目。",
               "原句的位置由程序核对（忽略网页排版符号、空白、标点、繁简差异）；是否等价、独立来源是否支持由审查者判断。",
               "农历日期由程序按法鼓文理学院时间规范资料库（CC BY-SA 3.0）换算；1582 年 10 月 15 日以前用儒略历。"
               "生肖按出生当天所在农历年的干支确定，不再排除 1—3 月出生者。",
               "第一期未实现找茬者与准入门；结论不含找茬环节。"],
        "账本完整性": {"完整": ok, "第一个不一致事件": bad, "事件数": n},
    }
    out = project.root / "outputs"
    out.mkdir(exist_ok=True)
    (out / "结论.json").write_text(json.dumps(conclusion, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# 结论：{conclusion['问题']}", "", f"口径：{conclusion['口径']}"]
    groups = []
    for r in rows:
        if r["分组"] not in groups:
            groups.append(r["分组"])
    for g in groups:
        md += ["", f"## {g}" if g else "## 逐条结果", "",
               "| 条目 | 人物 | 结论 | 出生日期 | 农历年 | 独立验证次数 | 生肖 | 说明 |", "|---|---|---|---|---|---|---|---|"]
        for r in rows:
            if r["分组"] != g:
                continue
            md.append(f"| {r['条目']} | {r['人物']} | {r['结论']} | {r['出生日期'] or ''} | {r['农历年干支'] or ''} | {r['独立验证次数']} | "
                      f"{r['生肖'] or ''} | {r['生肖说明']}；{r['备注'] or ''} |")
    md += ["", "## 生肖统计（全部分组合计）"]
    md += ["", f"已确定生肖：{stats['已确定生肖人数']} 人；未能确定：{stats['未能确定人数']} 人", "",
           "| 生肖 | 人数 | 占已确定者比例 |", "|---|---|---|"]
    md += [f"| {g['生肖']} | {g['人数']} | {g['占已确定者比例'] if g['占已确定者比例'] is not None else ''} |"
           for g in stats["分组"]]
    md += ["", "说明："] + [f"- {x}" for x in conclusion["说明"]]
    md += ["", f"账本完整性：{'完整' if ok else '不完整，第一个不一致事件 ' + str(bad)}（共 {n} 条事件）"]
    (out / "结论.md").write_text("\n".join(md), encoding="utf-8")
    project.log("程序", "输出结论", content={"file": "outputs/结论.json"})
    return conclusion
