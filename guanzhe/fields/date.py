"""日期字段的程序核对器（1.7 从主流程里拆出来；逻辑与 1.6 相同）。

一个题目包里的"日期"字段（如 出生日期）交给它核对：
- 公历写法（YYYY-MM-DD 或只写年份 YYYY）；
- 农历插件：原文用年号纪年的农历写法（字段"农历原文"），由程序按历表换算公历，不交给模型算；
- 各方是否一致（儒略历与格里历外推、只写年份与完整日期的相容处理）；
- 给审查者的"等价写法"，以及从独立来源段落里提取日期；
- 农历年干支（题目包可用来做生肖统计）。

主流程只调用这里的方法，不再直接处理日期。
"""

from __future__ import annotations

import re

from .. import blocks as B
from .. import cn_calendar as CAL
from ..textnorm import clean_md, find_skeleton

_DATE = re.compile(r"^(\d{3,4})(?:-(\d{1,2})-(\d{1,2}))?$")


def normalize_date(s):
    m = _DATE.match(str(s or "").strip())
    if not m:
        return None
    y = int(m.group(1))
    if m.group(2):
        return (y, int(m.group(2)), int(m.group(3)))
    return (y, None, None)


def format_date(d):
    y, m, dd = d
    return f"{y}" if m is None else f"{y}-{m:02d}-{dd:02d}"


class DateField:
    """key：字段名（如"出生日期"）；noun：事件的叫法（如"出生"），用于提示文字和程序生成的检索词；
    marks：在独立来源里找这个事件的用字（正则），日期须出现在这些字附近 40 字以内。"""

    kind = "日期"
    lunar_key = "农历原文"

    def __init__(self, key="出生日期", noun="出生", marks=r"生|诞|誕"):
        self.key, self.noun, self.marks = key, noun, marks

    # ---------- 基本 ----------
    normalize = staticmethod(normalize_date)
    format = staticmethod(format_date)

    def value(self, fields):
        return (fields or {}).get(self.key)

    def is_partial(self, d):
        """只有年份、没有月日。"""
        return d[1] is None

    def display(self, f):
        """窗口群聊里显示这一栏的写法。"""
        v = f.get(self.key) or ""
        if f.get("程序换算"):
            v = f"{v}（程序按农历「{f.get(self.lunar_key) or f.get('程序识别农历') or ''}」换算）"
        return v

    def stall_keys(self):
        """判断"两轮提交一模一样"时要比的字段。"""
        return (self.key, self.lunar_key)

    # ---------- 提交检查（原句已由主流程确认在原文里之后） ----------
    def check(self, f, raw, hint):
        """返回 (标记列表, 查证状态)；会就地补上 程序换算、农历年干支 等字段，找不到的农历原文会被剔除。"""
        flags = []
        # 农历：模型只照抄原文写法，公历由程序按历表换算
        lunar_txt = (f.get(self.lunar_key) or "").strip()
        conv = None
        if not lunar_txt:  # 生产者没填农历原文，但原句里有年号农历写法：程序自己识别出来核对
            parsed = CAL.parse(clean_md(f.get("原句", "")))
            if parsed and parsed.get("day") and not parsed["arabic"]:
                lunar_txt = parsed["text"]
                f["程序识别农历"] = lunar_txt
        if lunar_txt:
            parsed, conv, why = self.lunar_check(lunar_txt, clean_md(raw), clean_md(f.get("原句", "")), hint)
            if why == "剔除":
                # 1.5：农历原文在所引原文里找不到，多半是模型凭记忆写的：整段剔除，不交给审查者
                f.pop(self.lunar_key, None)
                flags.append("农历原文未在原文中找到（疑为模型记忆），已剔除")
            elif why:
                flags.append(why)
        filled = (f.get(self.key) or "").strip()
        if conv:
            f["程序换算"] = f"{conv['date']}（{conv['calendar']}，农历{conv['year_ganzhi']}年）"
            f["农历年干支"] = conv["year_ganzhi"]
            if conv.get("supplied"):
                f["程序换算"] += "；年号由生产者补出，干支与历表一致"
                flags.append(f"年号由生产者补出，干支与历表一致（原文只写了{parsed['gz']}年）")
            alt = CAL.proleptic_gregorian(conv["jd"])
            if not filled:
                f[self.key] = conv["date"]
                flags.append(f"{self.key}由程序按农历换算：{f['程序换算']}")
            elif filled == conv["date"]:
                pass
            elif filled == alt and alt != conv["date"]:
                f[self.key] = conv["date"]
                flags.append(f"所填 {filled} 是按格里历外推的写法，已按惯例改为 {conv['date']}（儒略历）")
            else:
                d = normalize_date(filled)
                if (d and d[1] is not None and parsed.get("paren_year")
                        and (d[1], d[2]) == (parsed["month"], parsed["day"])):
                    # "洪武十一年（1378年）八月十六日"这类写法：所填公历正好等于写出的月日，原文可能本来就是公历月日
                    for k in ("程序换算", "农历年干支"):
                        f.pop(k, None)
                    flags.append(f"原文\"{parsed['text']}\"的月日可能本来就是公历（所填 {filled} 与之相同；"
                                 f"若按农历则为 {conv['date']}），程序无法判定，交审查者核对")
                    return flags, "待审查"
                if d and d[1] is None and d[0] == int(conv["date"][:4]):
                    f[self.key] = conv["date"]
                    flags.append(f"所填只有年份，按农历换算补全为 {conv['date']}")
                else:
                    flags.append(f"所填公历 {filled} 与农历换算结果 {conv['date']} 不符")
                    return flags, "换算不符"
            return flags, "待审查"
        if not filled or not normalize_date(filled):
            return flags + [f"没有可用的{self.key}（公历未填或格式不对，也没有可换算的农历原文）"], "未查证"
        date = normalize_date(filled)
        qnums = B.numbers_in(f.get("原句", ""))
        if not any(int(x) == date[0] for x in qnums if x == int(x)):
            flags.append(f"{self.noun}年份的数字未出现在原句中")
        return flags, "待审查"

    def lunar_check(self, lunar_txt, raw_c, quote_c, hint):
        """1.5：核对农历原文并换算。返回 (识别结果, 换算结果, 说明)。
        说明为 "剔除" 表示原文里找不到这段农历写法；为 None 表示正常换算；其余为标记文字（未作换算）。
        "在原文里"的判断：逐字（忽略标点繁简）找到；或原文里有同一个 年号·年·月·日（允许中间夹括号注释）；
        或生产者补了年号——原文里有同一个 干支·月·日，且补的年号那一年的干支与之相同。"""
        lt = clean_md(lunar_txt)
        parsed, conv = CAL.from_text(lt, hint)
        key = lambda d: (d.get("month"), d.get("day"), d.get("leap"))
        literal = bool(find_skeleton(raw_c, lt) or find_skeleton(quote_c, lt))
        supplied = False
        if not literal and parsed and parsed.get("day"):
            same = [d for d in CAL.era_dates(raw_c) + CAL.era_dates(quote_c)
                    if (d["era"], d["year"]) == (parsed["era"], parsed["year"]) and key(d) == key(parsed)]
            literal = bool(same)
            if not literal and parsed.get("gz"):
                same_gz = [d for d in CAL.ganzhi_dates(raw_c) + CAL.ganzhi_dates(quote_c)
                           if d["gz"] == parsed["gz"] and key(d) == key(parsed)]
                supplied = bool(same_gz)
        if not literal and not supplied:
            if not parsed:
                gzs = CAL.ganzhi_dates(lt)
                if gzs and any(g["gz"] == x["gz"] and key(g) == key(x)
                               for g in gzs for x in CAL.ganzhi_dates(raw_c) + CAL.ganzhi_dates(quote_c)):
                    return None, None, ("农历原文只有干支纪年、没有年号，程序无法确定是哪一年，未作换算。"
                                        f"可在前面补上年号（如\"某某元年{gzs[0]['text']}日\"），程序会核对干支后换算")
            return parsed, None, "剔除"
        if not parsed:
            gzs = CAL.ganzhi_dates(lt)
            if gzs:
                return None, None, ("农历原文只有干支纪年、没有年号，程序无法确定是哪一年，未作换算。"
                                    f"可在前面补上年号（如\"某某元年{gzs[0]['text']}日\"），程序会核对干支后换算")
            return None, None, "农历原文里没有识别出\"年号+年+月+日\"，未作换算"
        if parsed["arabic"]:
            return parsed, None, "农历原文的月日是阿拉伯数字，像是公历写法，未按农历换算"
        if not conv or not conv.get("date"):
            return parsed, None, "历表中查不到这个农历日期（年号、月份或日子可能有误），未作换算"
        if parsed.get("gz") and parsed["gz"] != conv["year_ganzhi"]:
            return parsed, None, (f"农历原文写的干支是{parsed['gz']}，但{parsed['era']}{parsed['year']}年在历表中是"
                                  f"{conv['year_ganzhi']}年，对不上，未作换算")
        if supplied:
            conv = dict(conv, supplied=True)
        return parsed, conv, None

    # ---------- 审查 ----------
    def review_values(self, f):
        """交给审查者看的本字段内容（按 1.6 的顺序）。"""
        return {self.key: f.get(self.key), self.lunar_key: f.get(self.lunar_key), "程序换算": f.get("程序换算")}

    equivalents_label = "所填日期的等价写法"

    def equivalents(self, value, hint):
        d = normalize_date(value)
        if not d:
            return None
        if d[1] is None:
            return {"只知年份": str(d[0])}
        try:
            return CAL.equivalents(*d, hint)
        except (ValueError, KeyError, IndexError):
            return None

    def evidence(self, text, hint):
        """独立来源段落里这个事件附近的日期（程序提取），返回可能的儒略日编号集合，提不出返回 None。"""
        t = text or ""
        marks = [m.start() for m in re.finditer(self.marks, t)]
        for pos, js in CAL.dates_in(t, hint):
            if any(abs(pos - m) <= 40 for m in marks):
                return js
        return None

    def matches(self, value, evidence):
        """所填的值与独立来源里提取出的值是否是同一天。"""
        d = normalize_date(value)
        if not d or d[1] is None or not evidence:
            return False
        return CAL.date_to_jd(*d) in evidence

    # ---------- 各方一致 ----------
    @staticmethod
    def agree(dates):
        """各方日期是否一致（1.4：同一年份的"只有年份"与"完整日期"视为相容；1582 年以前按儒略历和按格里历外推
        写的同一天视为一致，统一成儒略历写法）。返回合并后的日期或 None。"""
        full = {d for d in dates if d[1] is not None}
        if len(full) > 1:
            jds = {}
            for d in full:
                js = {CAL.date_to_jd(*d)}
                if d < (1582, 10, 15):
                    js.add(CAL.date_to_jd(*d, "格里历"))
                jds[d] = js
            common = set.intersection(*jds.values())
            if len(common) == 1:
                y, m, dd, _ = CAL.jd_to_date(next(iter(common)))
                full = {(y, m, dd)}
                dates = [(y, m, dd) if d[1] is not None else d for d in dates]
        years = {d[0] for d in dates}
        if len(years) != 1 or len(full) > 1:
            return None
        return next(iter(full)) if full else next(iter(dates))

    # ---------- 农历年（题目包用来做生肖统计） ----------
    def category(self, fields_list, d, hint):
        """所在农历年的干支：优先用提交检查时换算出的，没有就按公历日期查历表。"""
        for f in fields_list:
            gz = f.get("农历年干支")
            if gz:
                return gz
        if d and d[1] is not None:
            try:
                return CAL.lunar_year_of_jd(CAL.date_to_jd(*d), hint)
            except (KeyError, ValueError, OSError):
                return None
        return None

    def category_of_evidence(self, jd, hint):
        return CAL.lunar_year_of_jd(jd, hint)
