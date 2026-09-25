"""把账本事件翻译成"群聊消息"，供窗口版显示（1.3）。只读，不写账本。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from . import tasks
from .tasks.fact_compilation import FIELD as _DEFAULT_FIELD

FIELD = _DEFAULT_FIELD

ROLE_KIND = {"生产者": "producer", "审查者": "reviewer"}


def _j(s):
    try:
        return json.loads(s) if s else {}
    except ValueError:
        return {}


def _connect(db):
    c = sqlite3.connect(str(db), timeout=5)
    c.row_factory = sqlite3.Row
    return c


def role_names(root: Path) -> dict:
    """角色编号 -> {name, model, kind}；来自项目里保存的设置快照（不含密钥）。"""
    out = {}
    snap = root / "settings.snapshot.json"
    if snap.exists():
        try:
            st = json.loads(snap.read_text(encoding="utf-8-sig"))
            for r, s in st.get("roles", {}).items():
                out[r] = {"name": s.get("series") or r, "model": s.get("model", ""),
                          "kind": s.get("kind", "")}
        except ValueError:
            pass
    return out


def _purpose(call_id: str) -> str:
    # 形如 R1-P1-search#1、R2-P2-write-1#1、R1-R1-review-P2-1#1、ping-P1#1
    body = call_id.split("#")[0]
    if "-search" in body:
        return "想好了要检索什么"
    if "-write" in body:
        return "写好了本轮的结论"
    if "-review-" in body:
        who = body.split("-review-")[1].split("-")[0]
        return f"审查完 {{{who}}} 的结论"
    return "回复了"


def messages(root: Path, after: int = 0, limit: int = 400) -> dict:
    root = Path(root)
    db = root / "project.db"
    if not db.exists():
        return {"messages": [], "last": after}
    names = role_names(root)
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {}
    item_name = {x["id"]: x.get("name", x["id"]) for x in cfg.get("items", [])}
    global FIELD
    try:
        FIELD = tasks.load(cfg).FIELD
    except ValueError:
        FIELD = _DEFAULT_FIELD
    c = _connect(db)
    try:
        evs = c.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
        out = []
        last = after
        for e in evs:
            last = e["seq"]
            m = _render(c, e, names, item_name)
            if m:
                m.update({"seq": e["seq"], "ts": e["ts"], "event": e["event_id"], "round": e["round"]})
                out.append(m)
        return {"messages": out, "last": last}
    finally:
        c.close()


def _names_in(text, names):
    import re
    return re.sub(r"\{(\w+)\}", lambda m: who(m.group(1), names), text)


def _codes(text, names):
    import re
    for r in sorted(names, key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(r)}(?![A-Za-z0-9])", names[r]["name"], text)
    return text


def who(role, names):
    if role in names:
        return names[role]["name"]
    return {"程序": "程序", "用户": "你"}.get(role, role)


def _render(c, e, names, item_name):
    t, actor, content = e["type"], e["actor"], _j(e["content"])
    ref = e["ref_id"]

    def msg(speaker_role, text, kind="normal"):
        return {"role": speaker_role, "speaker": who(speaker_role, names),
                "tag": ("审查" if names.get(speaker_role, {}).get("kind") == "审查者" else
                        "生产" if names.get(speaker_role, {}).get("kind") == "生产者" else ""),
                "text": text, "kind": kind}

    if t == "项目创建":
        return msg("程序", "项目已创建。", "system")
    if t == "配置":
        roles = content.get("roles", {})
        line = "；".join(f"{r}＝{v.get('series', r)}（{v.get('model', '')}，{v.get('kind', '')}）" for r, v in roles.items())
        return msg("程序", f"本项目的分工：{line}", "system")
    if t == "准入判断":
        return msg("用户", "放行（第一期由你手动放行）。", "system")
    if t == "模型调用":
        if content.get("status") == "成功":
            return msg(actor, _names_in(_purpose(ref or ""), names), "quiet")
        return msg(actor, f"调用没成功：{content.get('failure') or '未知原因'}（程序会自动重试）", "warn")
    if t == "检索":
        r = c.execute("SELECT query,url,title,item_id FROM retrievals WHERE retrieval_id=?", (ref,)).fetchone()
        if r:
            host = urlparse(r["url"] or "").netloc
            where = "（只在可信网站内）" if content.get("限定网站") else ""
            return msg("程序", f"替 {who(content.get('requester', ''), names)} 检索{where}「{r['query']}」，存档：{r['title'] or ''}（{host}）", "quiet")
        return None
    if t == "检索失败":
        return msg("程序", f"检索失败：{content}", "warn")
    if t == "提交检查":
        b = c.execute("SELECT author,item_id,fields FROM blocks WHERE block_id=?", (ref,)).fetchone()
        if not b:
            return None
        f = _j(b["fields"])
        name = item_name.get(b["item_id"], b["item_id"])
        flags = content.get("标记") or []
        state = f.get("状态") or ""
        date = FIELD.display(f)  # 1.8：按题目包声明的字段类型显示
        quote = f.get("原句") or ""
        src = f.get("出处") or ""
        text = f"{name}：{date or state}"
        if date and state and state not in ("查到", date):
            text += f"（{state}）"
        if quote:
            text += f"\n原句：「{quote}」"
        if src:
            text += f"　出处 {src}"
        m = msg(b["author"], text, "claim")
        if flags:
            bad = [x for x in flags if "未在" in x or "不符" in x or "不存在" in x or "未找到" in x
                   or "不是你自己" in x or "没有可用" in x]
            m["check"] = {"ok": None if not bad else False, "text": "；".join(flags)}
        else:
            m["check"] = {"ok": True, "text": "程序核对：原句确实在存档原文里，交给审查者"}
        return m
    if t == "拒收":
        return msg("程序", f"拒收：{content}", "warn")
    if t == "审查意见":
        blk = content.get("块", "")
        b = c.execute("SELECT item_id FROM blocks WHERE block_id=?", (blk,)).fetchone()
        rv = c.execute("SELECT reasoning FROM reviews WHERE block_id=? AND reviewer=? ORDER BY rowid DESC LIMIT 1",
                       (blk, actor)).fetchone()
        name = item_name.get(b["item_id"], "") if b else ""
        author = blk.split("-")[0] if blk else ""
        if content.get("等价") or content.get("独立核验"):
            text = (f"{who(author, names)} 的「{name}」：原句与所填日期{content.get('等价') or '—'}；"
                    f"独立核验：{content.get('独立核验') or '—'}"
                    + (f"（依据 {content['独立来源']}）" if content.get("独立来源") else ""))
        else:
            text = f"{who(author, names)} 的「{name}」：{content.get('判定', '')}"
        if content.get("意见"):
            text += f"\n意见：{content['意见']}"
        elif rv and rv["reasoning"]:
            text += f"\n理由：{rv['reasoning']}"
        kind = "review_ok" if content.get("判定") == "正确" and content.get("独立核验") != "矛盾" else "review_bad"
        if content.get("同模型审查"):
            text += "\n（注意：这是同一家模型审自己）"
        return msg(actor, text, kind)
    if t == "意见提出":
        o = c.execute("SELECT target_author FROM opinions WHERE opinion_id=?", (ref,)).fetchone()
        to = who(o["target_author"], names) if o else ""
        text = _codes(content.get('内容', ''), names)
        for iid, nm in item_name.items():
            text = text.replace(f"条目 {iid}：", f"「{nm}」：").replace(f"条目 {iid} ", f"「{nm}」")
        return msg("程序", f"@{to}　{text}", "opinion")
    if t == "意见处理":
        text = f"回应意见：{content.get('处理', '')}。{content.get('理由', '')}"
        return msg(actor, text, "normal")
    if t == "意见闭合":
        return None
    if t == "状态更新":
        return msg("程序", f"本轮小结：改动 {content.get('改动块', 0)} 处，新发现错误 {content.get('新发现错误', 0)} 个，"
                          f"还没处理完的意见 {content.get('未闭合意见', 0)} 条。", "system")
    if t == "轮次完成":
        return msg("程序", f"第 {e['round']} 轮结束", "banner")
    if t == "迭代结束":
        return msg("程序", f"全部结束：{content.get('原因', '')}", "banner")
    if t == "调用失败暂停":
        return msg("程序", f"暂停了：{content}。处理好以后点「继续运行」，已完成的部分不会重复花钱。", "warn")
    if t == "输出结论":
        return msg("程序", "结论已生成，点下方「看结论」查看。", "banner")
    if t in ("分发", "对照", "提交产物", "回流登记", "摘要", "找茬者未出场", "存档答复不适用",
             "配置一致性提示", "重做未完成轮次"):
        return None
    if t == "部分提交":
        miss = "、".join(item_name.get(i, i) for i in content.get("本轮没交的条目") or [])
        return msg(actor if actor in names else "程序",
                   f"{who(ref, names)} 这一批有条目没交或交重了：先收下交对的" + (f"，本轮没交：{miss}（下一轮再补）" if miss else ""), "quiet")
    if t == "缺可信网站名单":
        return msg("程序", content.get("说明", "题目文件里没有可信网站名单"), "alert")
    if t == "配置变更":
        return msg("用户", "角色设置和上次运行时不同，新调用使用新设置。", "system")
    return msg(actor if actor in names else "程序", f"{t}", "quiet")
