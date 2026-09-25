"""历史农历换算（1.4）：年号纪年的农历日期 → 西历日期，由程序计算，不交给模型。

数据：法鼓文理学院（DDBC/DILA）时间规范资料库 authority_time 2012-02 版，许可 CC BY-SA 3.0，
见 data/cn_calendar_COPYING.txt。数据以月为单位，记录每个农历月第一天的儒略日编号。

西历惯例（与史学界及维基百科一致）：儒略日编号 < 2299161（1582-10-15）用儒略历，此后用格里历。
生肖按农历年的干支地支确定。
"""

from __future__ import annotations

import gzip
import json
import re
from functools import lru_cache
from pathlib import Path

from .textnorm import t2s

GREGORIAN_START = 2299161
ZODIAC = dict(zip("子丑寅卯辰巳午未申酉戌亥", "鼠牛虎兔龙蛇马羊猴鸡狗猪"))
_NUM = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


@lru_cache(maxsize=1)
def _data():
    p = Path(__file__).parent / "data" / "cn_calendar.json.gz"
    return json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))


@lru_cache(maxsize=1)
def _index():
    """简体年号名 -> [(朝代, 月列表)]"""
    idx = {}
    for era in _data()["eras"].values():
        for n in era["names"]:
            idx.setdefault(t2s(n), []).append((era["dyn"], era["m"]))
    return idx


def cn_number(s: str):
    """中文数字（含 初、廿、卅、元）或阿拉伯数字 -> int"""
    if s is None:
        return None
    s = s.strip().replace("初", "")
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in ("元", "正"):
        return 1
    s = s.replace("廿", "二十").replace("卅", "三十")
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        return (_NUM.get(a, 1) if a else 1) * 10 + (_NUM.get(b, 0) if b else 0)
    if all(c in _NUM for c in s):
        v = 0
        for c in s:
            v = v * 10 + _NUM[c]
        return v
    return None


_MONTH_ALIAS = {"正": 1, "冬": 11, "腊": 12, "臘": 12}
_GZ = "[甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥]"
_MD = (r"[，,、\s]*(?P<leap>闰|閏)?(?P<m>正|冬|腊|臘|十[一二]?|[一二三四五六七八九]|\d{1,2})月"
       r"(?P<d>初[一二三四五六七八九十]|[一二三]?十[一二三四五六七八九]?|廿[一二三四五六七八九]?|卅|[一二三四五六七八九]|\d{1,2})?")
_PAT = re.compile(
    r"(?P<era>[一-鿿]{2,4}?)"
    r"(?:(?P<y>元|[〇零一二三四五六七八九十廿卅]{1,4}|\d{1,2})年"
    r"(?:\s*[（(][^）)]{0,14}[）)])?"
    rf"(?:[，,、\s]*(?P<gz>{_GZ})(?:年|岁|歲)?)?"
    r"(?:\s*[（(][^）)]{0,14}[）)])?"
    rf"|(?P<gz2>{_GZ})(?:年|岁|歲))"  # 1.5：年号后直接跟"干支＋岁/年"，如"建文己卯岁"
    + _MD
)
# 1.5：只有干支纪年、没有年号的写法，如"己卯岁二月九日"
_GZPAT = re.compile(rf"(?P<gz>{_GZ})(?:年|岁|歲)" + _MD)


def _era_years_with_gz(era: str, gz: str):
    """某年号里干支为 gz 的年份（可能跨朝代同名年号，返回去重后的集合）。"""
    return {y for _, months in _index().get(t2s(era), []) for y, mo, lp, f, l, st, g in months if g == gz}


def parse(text: str):
    """从一段文字里找出"年号+年+月+日"的农历写法。返回 dict 或 None。
    1.5：年号后可以是"干支＋岁/年"（不写第几年）；写了干支的，另给出 gz（原文写的干支）。"""
    if not text:
        return None
    for m in _PAT.finditer(t2s(text)):
        era = m.group("era")
        cands = [era[i:] for i in range(len(era) - 1)]  # 允许前面多出的字，如"明建文"
        name = next((c for c in cands if c in _index()), None)
        if not name:
            continue
        gz = m.group("gz") or m.group("gz2")
        if m.group("y"):
            year = cn_number(m.group("y"))
        else:
            ys = _era_years_with_gz(name, gz)
            year = ys.pop() if len(ys) == 1 else None
        mm = _MONTH_ALIAS.get(m.group("m")) or cn_number(m.group("m"))
        return {"era": name, "year": year, "month": mm, "gz": gz,
                "leap": bool(m.group("leap")), "day": cn_number(m.group("d")) if m.group("d") else None,
                "text": m.group(0),
                # 中文网页常把"年号纪年 + 公历月日"混写（如"正德二年（1507年）9月16日"）：
                # 月日用阿拉伯数字的，按公历写法处理；年后带括号公历年份的，月日是农历还是公历无法从写法判断
                "arabic": bool(re.fullmatch(r"\d+", m.group("m")) or (m.group("d") and m.group("d").isdigit())),
                "paren_year": bool(re.search(r"[（(]\s*\d{3,4}\s*年?\s*[）)]", m.group(0)))}
    return None


def ganzhi_dates(text: str):
    """1.5：文字里所有"干支＋岁/年＋月＋日"的写法（不管前面有没有年号）。返回 [{gz, month, day, leap, text}]。"""
    out = []
    for m in _GZPAT.finditer(t2s(text or "")):
        if re.fullmatch(r"\d+", m.group("m")) or (m.group("d") and m.group("d").isdigit()):
            continue
        out.append({"gz": m.group("gz"), "month": _MONTH_ALIAS.get(m.group("m")) or cn_number(m.group("m")),
                    "day": cn_number(m.group("d")) if m.group("d") else None,
                    "leap": bool(m.group("leap")), "text": m.group(0)})
    return out


def era_dates(text: str):
    """1.5：文字里所有"年号＋年＋月＋日"写法的解析结果（用于判断农历写法是否真在原文里，允许中间夹括号注释）。"""
    out = []
    for m in _PAT.finditer(t2s(text or "")):
        p = parse(m.group(0))
        if p:
            out.append(p)
    return out


def jd_to_date(jd: int):
    """儒略日编号 -> (年, 月, 日, 历法名)，按惯例：1582-10-15 以前用儒略历。"""
    if jd < GREGORIAN_START:
        c = jd + 32082
        d = (4 * c + 3) // 1461
        e = c - (1461 * d) // 4
        m = (5 * e + 2) // 153
        return d - 4800 + m // 10, m + 3 - 12 * (m // 10), e - (153 * m + 2) // 5 + 1, "儒略历"
    a = jd + 32044
    b = (4 * a + 3) // 146097
    c = a - 146097 * b // 4
    d = (4 * c + 3) // 1461
    e = c - 1461 * d // 4
    m = (5 * e + 2) // 153
    return 100 * b + d - 4800 + m // 10, m + 3 - 12 * (m // 10), e - (153 * m + 2) // 5 + 1, "格里历"


def convert(era: str, year: int, month: int, day, leap: bool = False, dynasty_hint: str | None = None):
    """年号农历 -> 结果列表（同名年号可能属于不同朝代）。每项：
    {dynasty, date: 'YYYY-MM-DD' 或 None, calendar, year_ganzhi, zodiac, jd}"""
    out = []
    cands = _index().get(t2s(era), [])
    if dynasty_hint and any(t2s(dynasty_hint) in t2s(d) or t2s(d) in t2s(dynasty_hint) for d, _ in cands):
        # 1.6：朝代提示只用来在同名年号之间挑；本朝没有这个年号时（如朱元璋生于元"天历"、努尔哈赤生于明"嘉靖"）不排除别的朝代
        cands = [(d, m) for d, m in cands if t2s(dynasty_hint) in t2s(d) or t2s(d) in t2s(dynasty_hint)]
    for dyn, months in cands:
        for y, mo, lp, first, last, start, gz in months:
            if y == year and mo == month and bool(lp) == bool(leap):
                res = {"dynasty": dyn, "year_ganzhi": gz, "zodiac": ZODIAC.get(gz[1:2]) if gz else None,
                       "date": None, "calendar": None, "jd": None}
                if day:
                    jd = first + day - (start or 1)
                    if jd <= last:
                        Y, M, D, cal = jd_to_date(jd)
                        res.update(date=f"{Y:04d}-{M:02d}-{D:02d}", calendar=cal, jd=jd)
                out.append(res)
    return out


def from_text(text: str, dynasty_hint: str | None = None):
    """便捷函数：在文字中识别农历写法并换算。返回 (识别结果, 换算结果) 或 (None, None)。"""
    p = parse(text)
    if not p or not p["year"] or not p["month"]:
        return p, None
    res = convert(p["era"], p["year"], p["month"], p["day"], p["leap"], dynasty_hint)
    return p, (res[0] if len(res) == 1 else (res[0] if res and all(r["date"] == res[0]["date"] for r in res) else None))


def date_to_jd(y: int, m: int, d: int, calendar: str | None = None) -> int:
    """西历日期 -> 儒略日编号。calendar 为 None 时按惯例（1582-10-15 起格里历，之前儒略历）。"""
    a = (14 - m) // 12
    yy, mm = y + 4800 - a, m + 12 * a - 3
    greg = ((y, m, d) >= (1582, 10, 15)) if calendar is None else (calendar == "格里历")
    if greg:
        return d + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - yy // 100 + yy // 400 - 32045
    return d + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - 32083


def proleptic_gregorian(jd: int) -> str:
    """按格里历外推写出的日期（有些网站 1582 年以前也用格里历写）。"""
    a = jd + 32044
    b = (4 * a + 3) // 146097
    c = a - 146097 * b // 4
    d = (4 * c + 3) // 1461
    e = c - 1461 * d // 4
    m = (5 * e + 2) // 153
    return f"{100 * b + d - 4800 + m // 10:04d}-{m + 3 - 12 * (m // 10):02d}-{e - (153 * m + 2) // 5 + 1:02d}"


def lunar_year_of_jd(jd: int, dynasty_hint: str | None = None):
    """某一天所在农历年的干支（取历表中覆盖这一天的月份；有朝代提示时优先该朝）。找不到返回 None。"""
    best = None
    for era in _data()["eras"].values():
        for y, mo, lp, first, last, start, gz in era["m"]:
            if first <= jd <= last and gz:
                if dynasty_hint and t2s(dynasty_hint) in t2s(era["dyn"]):
                    return gz
                best = best or gz
    return best


_CN_DIGIT = "〇一二三四五六七八九"


def _cn(n: int, day=False) -> str:
    if day and n <= 10:
        return "初" + ("十" if n == 10 else _CN_DIGIT[n])
    if n < 10:
        return _CN_DIGIT[n]
    if n < 20:
        return "十" + (_CN_DIGIT[n - 10] if n > 10 else "")
    if day and 20 < n < 30:
        return "廿" + _CN_DIGIT[n - 20]
    if n < 100:
        return _CN_DIGIT[n // 10] + "十" + (_CN_DIGIT[n % 10] if n % 10 else "")
    return "".join(_CN_DIGIT[int(c)] for c in str(n))


def lunar_of_jd(jd: int, dynasty_hint: str | None = None):
    """某一天的年号农历写法，如"天历元年九月十八"。有朝代提示且该朝有记录时只给该朝的；
    否则列出当时并行的各种年号写法（用"／"隔开）。找不到返回 None。"""
    hits, pref = [], []
    for era in _data()["eras"].values():
        for y, mo, lp, first, last, start, gz in era["m"]:
            if first <= jd <= last:
                day = jd - first + (start or 1)
                txt = t2s(f"{era['names'][0]}{'元' if y == 1 else _cn(y)}年{'闰' if lp else ''}"
                          f"{'正' if mo == 1 else _cn(mo)}月{_cn(day, True)}")
                (pref if dynasty_hint and t2s(dynasty_hint) in t2s(era["dyn"]) else hits).append(txt)
    out = list(dict.fromkeys(pref or hits))
    return "／".join(out) if out else None


def equivalents(y: int, m: int, d: int, dynasty_hint: str | None = None) -> dict:
    """同一天的几种写法（按惯例的西历、格里历外推、年号农历、农历年干支），供审查者比对，避免把写法不同当成矛盾。"""
    jd = date_to_jd(y, m, d)
    out = {"西历（按惯例）": f"{y:04d}-{m:02d}-{d:02d}（{'格里历' if jd >= GREGORIAN_START else '儒略历'}）"}
    if jd < GREGORIAN_START:
        out["若按格里历外推写"] = proleptic_gregorian(jd)
    lun = lunar_of_jd(jd, dynasty_hint)
    if lun:
        out["农历"] = lun
    gz = lunar_year_of_jd(jd, dynasty_hint)
    if gz:
        out["农历年干支"] = gz
    return out


_GREG = re.compile(r"(?<!\d)(\d{3,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]?|(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")


def dates_in(text: str, dynasty_hint: str | None = None):
    """找出一段文字里的日期（公历或年号农历），返回 [(在文中的位置, 可能的儒略日编号集合)]。
    1582 年以前的公历写法可能是儒略历也可能是格里历外推，两种都算。"""
    out = []
    t = t2s(text or "")
    for m in _GREG.finditer(t):
        y, mo, d = (int(x) for x in (m.groups()[:3] if m.group(1) else m.groups()[3:]))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        cands = {date_to_jd(y, mo, d)}
        if (y, mo, d) < (1582, 10, 15):
            cands.add(date_to_jd(y, mo, d, "格里历"))
        out.append((m.start(), cands))
    for m in _PAT.finditer(t):
        p = parse(m.group(0))
        if p and p["day"] and not p["arabic"]:
            res = convert(p["era"], p["year"], p["month"], p["day"], p["leap"], dynasty_hint)
            js = {r["jd"] for r in res if r.get("jd")}
            if js:
                out.append((m.start(), js))
    return sorted(out, key=lambda x: x[0])
