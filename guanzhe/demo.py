"""演示场景：模拟模型 + 模拟检索，不联网、不需要密钥、不花钱。

人物与网页内容均为虚构的测试夹具，不代表任何真实史实。场景中故意安排：
- 生产者 P2 第 1 轮对"测试乙帝"编造了一句原文里没有的原句（程序应标"原句未找到"并提出意见）；
- 两个来源对"测试丙帝"的出生日期说法不一（应登记为有争议并要求复核）；
- "测试丁帝"查不到资料（两方都应如实报资料缺载）；
- "测试乙帝"两方引用同一网站，另一可信网站独立支持（应为查到）；
- "测试戊帝"原文只有年号农历（生产者照抄农历原文，由程序换算公历、按农历年干支定生肖）。
可信网站（trusted.test）的网页只在审查者独立核验时才检索得到（hidden），用来演示独立核验。
年号"建文"借用真实年号，只为演示历表换算，人物仍是虚构的。
"""

from __future__ import annotations

import json
import re

PAGES = [
    {"url": "https://example.test/jia", "title": "测试甲帝（虚构）", "keywords": ["甲帝"],
     "text": "测试甲帝是虚构人物。\n测试甲帝生于1500年10月3日，卒于1560年。\n其余内容略。"},
    {"url": "https://another.test/jia2", "title": "测试甲帝年表（虚构）", "keywords": ["甲帝"],
     "text": "年表：测试甲帝，1500年10月3日出生。"},
    {"url": "https://example.test/yi", "title": "测试乙帝（虚构）", "keywords": ["乙帝"],
     "text": "测试乙帝，虚构人物。\n测试乙帝生于1523年6月15日。"},
    {"url": "https://example.test/bing-a", "title": "测试丙帝甲说（虚构）", "keywords": ["丙帝"],
     "text": "测试丙帝生于1540年8月1日。"},
    {"url": "https://example.test/bing-b", "title": "测试丙帝乙说（虚构）", "keywords": ["丙帝"],
     "text": "另有记载：测试丙帝生于1541年8月1日。"},
    {"url": "https://example.test/wu", "title": "测试戊帝（虚构）", "keywords": ["戊帝"],
     "text": "测试戊帝，[虚构人物](https://example.test/x)。\n| 出生 | 建文元年二月初九日 |\n其余内容略。"},
    {"url": "https://another.test/wu2", "title": "测试戊帝小传（虚构）", "keywords": ["戊帝"],
     "text": "小传：测试戊帝于建文元年二月初九日出生。"},
    {"url": "https://trusted.test/jia", "title": "测试甲帝（可信网站，虚构）", "keywords": ["甲帝"], "hidden": True,
     "text": "测试甲帝，1500年10月3日生。"},
    {"url": "https://trusted.test/yi", "title": "测试乙帝（可信网站，虚构）", "keywords": ["乙帝"], "hidden": True,
     "text": "测试乙帝，1523年6月15日出生。"},
    {"url": "https://trusted.test/wu", "title": "测试戊帝（可信网站，虚构）", "keywords": ["戊帝"], "hidden": True,
     "text": "测试戊帝，建文元年二月初九日生。"},
]
TRUSTED = ["trusted.test"]

ITEMS = [{"id": "I01", "name": "测试甲帝"}, {"id": "I02", "name": "测试乙帝"},
         {"id": "I03", "name": "测试丙帝"}, {"id": "I04", "name": "测试丁帝"}, {"id": "I05", "name": "测试戊帝"}]
_LUNAR = re.compile(r"建文元年二月初九日")


def _parse_evidence(user):
    """从撰写提示词里取出每个条目的检索编号和原文段落。"""
    ev = {}
    for sec in re.split(r"==== 条目 ", user)[1:]:
        iid = sec[:3]
        found = re.findall(r"【(S\d+)】[^\n]*\n([^【=]*)", sec)
        ev[iid] = [(rid, body.strip()) for rid, body in found]
    return ev


def _dates(text):
    return re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)


def _has_date(text):
    return bool(_dates(text) or _LUNAR.search(text))


def producer(which):
    """which='P1' 或 'P2'。P2 在第 1 轮编造乙帝原句；两方对丙帝各取一个来源。"""

    def respond(model, system, user):
        if '"searches"' in user and "请为需要检索的条目给出检索词" in user:
            todo = re.findall(r"- (I\d+)：", user.split("条目清单：")[1].split("\n\n")[0])
            return json.dumps({"searches": [{"item": it["id"], "query": it["name"] + " 出生"} for it in ITEMS
                                            if it["id"] in todo]}, ensure_ascii=False)
        rnd = int(re.search(r"第 (\d+) 轮", user).group(1))
        ev = _parse_evidence(user)
        ops = re.findall(r"- (O\d+)（条目 (I\d+)）", user)
        blocks = []
        for it in ITEMS:
            if f"==== 条目 {it['id']}" not in user:
                continue
            cands = [(rid, body) for rid, body in ev.get(it["id"], []) if _has_date(body)]
            other = None
            if it["id"] == "I03" and len(cands) >= 2:
                cands, other = (cands[:1], cands[1]) if which == "P1" else (cands[1:], cands[0])
            if not cands:
                blocks.append({"type": "条目", "item": it["id"],
                               "fields": {"人物": it["name"], "出生日期": "资料缺载", "状态": "资料缺载"}})
                continue
            rid, body = cands[-1] if (which == "P2" and it["id"] in ("I01", "I05")) else cands[0]
            line = next(l for l in body.splitlines() if _has_date(l))
            quote = line.strip()
            if not _dates(body):  # 原文只有农历：照抄农历原文，公历留空由程序换算
                blocks.append({"type": "条目", "item": it["id"], "fields": {
                    "人物": it["name"], "出生日期": "", "状态": "查到", "出处": rid, "原句": quote,
                    "农历原文": _LUNAR.search(body).group(0)}})
                continue
            y, m, d = _dates(body)[0]
            if which == "P2" and it["id"] == "I02" and rnd == 1:
                quote = "据载测试乙帝降生于1523年6月15日辰时"  # 编造的原句
            fields = {"人物": it["name"], "出生日期": f"{y}-{int(m):02d}-{int(d):02d}",
                      "状态": "查到", "出处": rid, "原句": quote}
            if other and rnd >= 2:  # 复核后如实报有争议，列出异说
                fields["状态"] = "有争议"
                fields["异说"] = f"另一来源 {other[0]} 记为 " + "-".join(_dates(other[1])[0])
            blocks.append({"type": "条目", "item": it["id"], "fields": fields})
        responses = []
        for oid, iid in ops:
            rid = next((r for r, b in ev.get(iid, []) if _has_date(b)), None)
            if rid:
                responses.append({"opinion": oid, "action": "采纳", "evidence": rid,
                                  "reason": "已重新核对原文并按原文摘抄"})
        return json.dumps({"blocks": blocks, "notes": "按口径逐条检索。",
                           "changes": [] if rnd == 1 else [{"type": "措辞", "item": blocks[0]["item"] if blocks else "I01",
                                                             "说明": "按原文重抄原句"}],
                           "responses": responses}, ensure_ascii=False)

    return respond


def reviewer(model, system, user):
    """模拟审查者：等价核对 + 独立核验，两项分开给。"""
    out = []
    for line in user.splitlines():
        if line.startswith("{") and '"block"' in line:
            b = json.loads(line)
            quote, date = b.get("原句") or "", b.get("出生日期") or ""
            if b.get("程序换算"):
                eq = "等价" if (b.get("农历原文") or "#") in quote.replace(" ", "") else "不等价"
            else:
                eq = "等价" if date[:4] and date[:4] in quote and b.get("人物", "") in (b.get("原文前后文") or "") else "不等价"
            ind, src = "无独立来源", ""
            srcs = b.get("独立来源") if isinstance(b.get("独立来源"), list) else []
            for s in srcs:
                txt = s["段落"]
                if (date[:4] and date[:4] in txt) or (b.get("农历原文") and b["农历原文"] in txt):
                    ind, src = "支持", s["编号"]
                    break
                if re.search(r"\d{4}年", txt):
                    ind, src = "矛盾", s["编号"]
            out.append({"block": b["block"], "equivalent": eq, "independent": ind, "independent_source": src,
                        "reason": "原句写的就是这个日期" if eq == "等价" else "原句不支持所填日期",
                        "severity": None if eq == "等价" else "必改"})
    return json.dumps({"reviews": out}, ensure_ascii=False)


def settings():
    return {
        "roles": {
            "P1": {"kind": "生产者", "provider": "mock", "model": "mock-a", "series": "模拟A", "context_window": 100000},
            "P2": {"kind": "生产者", "provider": "mock", "model": "mock-b", "series": "模拟B", "context_window": 100000},
            "R1": {"kind": "审查者", "provider": "mock", "model": "mock-c", "series": "模拟C", "context_window": 100000},
        },
        "search": {"engine": "mock"},
        "params": {"retry_batch1": [0, 0, 0], "retry_batch2": [0, 0, 0], "max_rounds": 6},
    }


def project_config():
    return {"project_id": "DEMO", "question": "列出测试皇帝的出生日期，并按生肖分组统计（演示，虚构数据）",
            "scope": "条目清单由用户给定；出生日期以公历记（1582 年 10 月 15 日以前用儒略历）；原文为农历的保留农历原文，公历由程序按历表换算；生肖按农历年干支由代码确定",
            "trusted_sites": TRUSTED, "items": ITEMS}


def providers():
    from .models import MockProvider
    return {"mock|": _Router()}


class _Router:
    """按模型名把调用分给不同的模拟角色。"""

    def __init__(self):
        self.map = {"mock-a": producer("P1"), "mock-b": producer("P2"), "mock-c": reviewer}

    def complete(self, model, system, user, max_tokens, timeout):
        text = self.map[model](model, system, user)
        return text, len(system + user) // 2, len(text) // 2
