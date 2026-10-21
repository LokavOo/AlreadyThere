"""把账本事件翻译成"群聊消息"，供窗口版显示（1.3）。只读，不写账本。"""

from __future__ import annotations

import datetime
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


def role_states(root: Path, running: bool) -> dict:
    """角色当前状态（在线/工作中/响应中/离线），供左栏头像用。

    2.5 按流式重做：
    - 数据在流（含保活）＝工作中，不管多久；第一次尝试不显示"第几次"。
    - 只有"确认出了问题"才是响应中（断连、网络错误、可重试的服务端错误，或者已经发生过重试），
      此时才显示"第几次重试"和问题原因；等收到新的真实内容后回到工作中。
    - 不可重试（欠费、密钥错误）＝离线（流程暂停后判定，附具体原因）。
    没在运行：最后一条账本事件如果是"调用失败暂停"，那个角色判离线，原因带上；否则都算在线。
    """
    root = Path(root)
    db = root / "project.db"
    if not db.exists():
        return {}
    c = _connect(db)
    try:
        states = {}
        if running:
            # 2.11：live_calls 每次调用一条；同一角色同时有几次调用时合并显示：
            # 有任何一次正常在收＝工作中；全部都确认出了问题才是响应中。秒数取最久的一次，字数合计，另给出每次调用的明细。
            now = datetime.datetime.now(datetime.timezone.utc)
            for r in c.execute("SELECT * FROM live_calls ORDER BY started_ts"):
                try:
                    started = datetime.datetime.fromisoformat(r["started_ts"])
                except (TypeError, ValueError):
                    started = now
                elapsed = int((now - started).total_seconds())
                one = {"key": r["call_key"], "label": call_label(r["call_key"]), "elapsed": elapsed,
                       "think": r["think_chars"] or 0, "out": r["out_chars"] or 0, "attempt": r["attempt"],
                       "of": r["of_total"], "problem": r["problem"], "note": r["note"]}
                st = states.setdefault(r["role"], {"calls": []})
                st["calls"].append(one)
            for role, st in states.items():
                cs = st["calls"]
                ok = [x for x in cs if not x["problem"]]
                st.update({"n": len(cs), "elapsed": max(x["elapsed"] for x in cs),
                           "think": sum(x["think"] for x in cs), "out": sum(x["out"] for x in cs),
                           "attempt": max(x["attempt"] or 1 for x in cs), "of": cs[0]["of"],
                           "notes": [x["label"] + "：" + x["note"] for x in cs if x["note"]]})
                if ok:
                    st["state"] = "工作中"
                else:
                    bad = max(cs, key=lambda x: x["attempt"] or 1)
                    st.update(state="响应中", retry=max(0, (bad["attempt"] or 1) - 1), problem=bad["problem"])
        else:
            last = c.execute("SELECT type, content FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            if last and last["type"] == "调用失败暂停":
                info = _j(last["content"])
                for f in (info.get("失败角色") or [{"role": info.get("role"), "reason": info.get("reason")}]):
                    if f.get("role"):
                        states[f["role"]] = {"state": "离线", "reason": f.get("reason") or "未知原因"}
        return states
    finally:
        c.close()


TASK_LABEL = {"p": "做题", "f": "拆表单", "fr": "审表单", "ff": "找茬（表单）", "frc": "复查（表单）", "fc": "找茬",
              "rc": "复查", "rv1": "逐份评审", "rvc": "对照审查", "rv2": "评审", "bare": "裸题对照", "ping": "连通测试"}


def call_label(call_key: str) -> str:
    """2.11：把调用键写成人看得懂的一句，例如 rv1-R2-A13-甲-r1 → 逐份评审 A13 甲 第1轮（角色名另外显示，这里不重复）。"""
    parts = (call_key or "").split("-")
    if not parts or parts[0] not in TASK_LABEL:
        return call_key or ""
    out = [TASK_LABEL[parts[0]]]
    for x in parts[2:]:
        if len(x) > 1 and x[0] == "r" and x[1:].isdigit():
            out.append(f"第{x[1:]}轮")
        elif x:
            out.append(x)
    return " ".join(out)


def live_thinking(root: Path) -> dict:
    """2.5：正在进行的调用的最新思考内容（只留最近一段，完整内容调用结束后在账本 calls.reasoning 里）。
    2.11：每次调用一个（以调用键为键），同一角色同时有几次调用就有几个。"""
    root = Path(root)
    db = root / "project.db"
    if not db.exists():
        return {}
    c = _connect(db)
    try:
        return {r["call_key"]: {"role": r["role"], "label": call_label(r["call_key"]), "text": r["think_tail"] or "",
                                "chars": r["think_chars"] or 0, "call": r["call_key"], "attempt": r["attempt"],
                                "started": r["started_ts"], "note": r["note"]}
                for r in c.execute("SELECT call_key, role, think_tail, think_chars, attempt, started_ts, note FROM live_calls "
                                   "WHERE COALESCE(think_chars,0)>0 ORDER BY started_ts")}
    finally:
        c.close()


END_KIND = {"迭代结束": None, "调用失败暂停": "调用失败暂停", "用户停止运行": "停止运行", "已暂停": "打断后的暂停"}


def timing(root: Path, running: bool = False) -> dict:
    """2.10：开始时间、结束时间、总用时（只算程序真正在跑的时间）；停下的时间按原因分开（用户 2026-10-03／10-21 定）。
    一次运行＝从"运行开始"到第一个结束事件（迭代结束／已暂停／调用失败暂停／用户停止运行）；"已暂停"前面紧挨着"直接打断"的，
    原因记为直接打断。没有结束事件就被下一次"运行开始"接上的（例如进程意外退出），按最后一条事件算结束，原因记"其他"。
    2.10 以前创建的项目没有"运行开始"记录，只能给出首尾时间，不区分停下的时间。"""
    import datetime as _dt
    root = Path(root)
    db = root / "project.db"
    if not db.exists():
        return {}
    c = _connect(db)
    try:
        evs = c.execute("SELECT seq, ts, type FROM events ORDER BY seq").fetchall()
    finally:
        c.close()

    def t(x):
        try:
            return _dt.datetime.fromisoformat(x)
        except (TypeError, ValueError):
            return None
    starts = [i for i, e in enumerate(evs) if e["type"] == "运行开始"]
    finished = next((e for e in evs if e["type"] == "迭代结束"), None)
    if not starts:
        calls = [t(e["ts"]) for e in evs if e["type"] == "模型调用"]
        if not calls:
            return {}
        st0 = min(calls)
        end = t(finished["ts"]) if finished else None
        return {"start": st0.isoformat(), "end": end.isoformat() if end else None,
                "run_seconds": None, "old": True}
    runs, stops = 0.0, {}
    for k, i in enumerate(starts):
        t0 = t(evs[i]["ts"])
        nxt = starts[k + 1] if k + 1 < len(starts) else len(evs)
        end_i, why = None, None
        for j in range(i + 1, nxt):
            ty = evs[j]["type"]
            if ty in END_KIND:
                end_i, why = j, END_KIND[ty]
                if ty == "已暂停" and j > 0 and evs[j - 1]["type"] in ("直接打断", "用户指令") and \
                        any(evs[x]["type"] == "直接打断" for x in range(i + 1, j)):
                    why = "直接打断"
                break
        if end_i is None:
            if k + 1 == len(starts) and running:  # 还在跑
                runs += max(0.0, (_dt.datetime.now(_dt.timezone.utc) - t0).total_seconds())
                continue
            end_i, why = nxt - 1, "其他"
        t1 = t(evs[end_i]["ts"])
        runs += max(0.0, (t1 - t0).total_seconds())
        if k + 1 < len(starts) and why:
            gap = (t(evs[starts[k + 1]]["ts"]) - t1).total_seconds()
            stops[why] = stops.get(why, 0.0) + max(0.0, gap)
        elif k + 1 == len(starts) and why and not running and not finished:  # 停着还没继续：停到现在
            stops[why] = stops.get(why, 0.0) + max(0.0, (_dt.datetime.now(_dt.timezone.utc) - t1).total_seconds())
    return {"start": evs[starts[0]]["ts"], "end": finished["ts"] if finished else None,
            "run_seconds": round(runs), "stops": {k: round(v) for k, v in stops.items()}, "old": False}


def call_thinking(root: Path, call_id: str) -> str | None:
    """2.5：某次调用的完整思考内容（来自账本）。"""
    root = Path(root)
    db = root / "project.db"
    if not db.exists():
        return None
    c = _connect(db)
    try:
        if call_id.startswith("unfinished:"):  # 2.11：被停止／直接打断的调用，思考存在"未完成调用"事件里
            r = c.execute("SELECT content FROM events WHERE seq=? AND type='未完成调用'",
                          (call_id.split(":", 1)[1],)).fetchone()
            return _j(r["content"]).get("思考（最近一段）") if r else None
        r = c.execute("SELECT reasoning FROM calls WHERE call_id=?", (call_id,)).fetchone()
        return r["reasoning"] if r else None
    finally:
        c.close()


def role_usage(root: Path) -> dict:
    """2.0 界面待办：左栏头像下方按角色显示的用量——token 输入/输出（失败的调用也算），
    生产者另加检索次数。数据直接从账本里的 calls/retrievals 表汇总，不是另外记的。"""
    root = Path(root)
    db = root / "project.db"
    if not db.exists():
        return {}
    c = _connect(db)
    try:
        out = {}
        for r in c.execute("SELECT role, SUM(COALESCE(input_tokens,0)) i, SUM(COALESCE(output_tokens,0)) o "
                            "FROM calls GROUP BY role"):
            out[r["role"]] = {"in": r["i"] or 0, "out": r["o"] or 0, "search": 0}
        for r in c.execute("SELECT requester, COUNT(*) n FROM retrievals GROUP BY requester"):
            out.setdefault(r["requester"], {"in": 0, "out": 0, "search": 0})
            out[r["requester"]]["search"] = r["n"]
        return out
    finally:
        c.close()


def _purpose(call_id: str) -> str:
    # 形如 R1-P1-search#1、R2-P2-write-1#1、R1-R1-review-P2-1#1、ping-P1#1
    body = call_id.split("#")[0]
    for pre, txt in (("f-", "交了表单（把题面拆成表单）"), ("fr-", "审完了各份表单"), ("ff-", "找完了表单里的茬"),
                     ("frc-", "复查完了找茬者对表单的问题"), ("fc-", "找完了答卷里的茬"), ("rc-", "复查完了找茬者的问题"),
                     ("rvc-", "对照完了各份答卷的摘要"), ("bare-", "答完了裸题")):
        if body.startswith(pre):
            return txt
    if body.startswith("rv1-") and body.count("-") >= 4:
        return "审完了一份答卷"
    if body.startswith("p-"):
        return "提交了答案和验证方案"
    if body.startswith("rv1-"):
        return "完成了四层比对审查"
    if body.startswith("rv2-"):
        return "完成了独立验证的比较"
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
                        "生产" if names.get(speaker_role, {}).get("kind") == "生产者" else
                        "找茬" if names.get(speaker_role, {}).get("kind") == "找茬者" else ""),
                "text": text, "kind": kind}

    if t == "项目创建":
        h = content.get("代码总哈希")
        txt = "项目已创建。"
        if h:
            txt += f"程序版本 {content.get('版本', '')}，代码总哈希 {h[:16]}…（共 {len(content.get('代码哈希') or {})} 个文件；同一个版本号对应唯一的内容）。"
        m = msg("程序", txt, "system")
        if h:
            m["codehash"] = {"version": content.get("版本"), "total": h, "files": content.get("代码哈希") or {}}
        return m
    if t == "配置":
        roles = content.get("roles", {})
        line = "；".join(f"{r}＝{v.get('series', r)}（{v.get('model', '')}，{v.get('kind', '')}）" for r, v in roles.items())
        return msg("程序", f"本项目的分工：{line}", "system")
    if t == "准入判断":
        if content.get("结果") == "用户提交":
            return msg("用户", f"提交了准入表：共 {content.get('题数')} 题，自检红 {content.get('红')}、黄 {content.get('黄')}；"
                             f"已知晓的红色问题 {len(content.get('已知晓的红色问题') or [])} 条。", "system")
        return msg("用户", "放行（第一期由你手动放行）。", "system")
    if t == "未完成调用":  # 2.11
        lab = call_label(content.get("call_key", ""))
        secs = content.get("已用秒数")
        m = msg(actor, f"{lab}：这次调用没有完成（{content.get('原因', '')}），已用 "
                       f"{secs if secs is not None else '?'} 秒，已收到思考 {content.get('思考字数', 0):,} 字、回答 "
                       f"{content.get('回答字数', 0):,} 字。已收到的内容存进了账本，只供查看，不参与审查。", "warn")
        if content.get("思考（最近一段）"):
            m["think"] = {"call": f"unfinished:{e['seq']}", "secs": secs, "chars": len(content["思考（最近一段）"]),
                          "total_secs": secs}
        return m
    if t == "用户停止运行":
        return msg("用户", "你停止了运行。已完成的内容都保留，点“继续运行”可以接着跑。", "system")
    if t == "代码运行设置":
        mid = "（运行中途改过设置）" if content.get("说明") else ""
        if content.get("允许运行代码"):
            libs = "、".join(f"{k} {v}" for k, v in (content.get("可用库") or {}).items() if v) or "无第三方库"
            return msg("程序", f"{mid}本次允许运行模型写的代码（{content.get('隔离级别')}）。本机可用的库：{libs}。", "system")
        return msg("程序", f"{mid}本次不运行任何代码（参数面板未勾选「允许运行模型写的代码」）：所有验证标为「未验证」。", "system")
    if t == "提交":  # 2.2 评审流程：生产者的提交
        rd = content.get("round", 1)
        stt = content.get("status") or "得出"
        shown = content.get("answer") if stt == "得出" else (f"【{stt}】" + (content.get("answer") or ""))
        if content.get("carried"):
            return msg(actor, f"{content.get('item')}（第 {rd} 轮）：没有收到针对我的意见，沿用上一轮：{shown}", "quiet")
        txt = f"{content.get('item')}（第 {rd} 轮{'，独立作答' if rd == 1 else '，修改后'}）：{shown}"
        u = content.get("unknown") or {}
        if stt != "得出" and u:
            txt += f"\n试过：{u.get('tried') or '—'}；卡在：{u.get('stuck') or '—'}；缺：{u.get('missing') or '—'}"
        if content.get("segments"):
            txt += "\n分段：" + "；".join(f"{x.get('condition')} → {x.get('value')}（{x.get('state')}）" for x in content["segments"])
        txt += f"\n推导：{content.get('reasoning') or ''}"
        for w in content.get("warnings") or []:
            txt += f"\n（程序提示：{w}）"
        ac = content.get("author_check") or {}
        if ac.get("failed") and stt != "未得出":
            txt += f"\n⚠ 作者自己的验证没有通过（验证代码{ac.get('status')}）却提交了答案：{ac.get('tail') or ''}"
        vp = content.get("verify_plan") or {}
        if vp:
            txt += f"\n验证方案：{vp.get('what', '')}（预期：{vp.get('expected', '')}；容差：{vp.get('tolerance', '')}）"
        for c in content.get("changes") or []:
            txt += f"\n改动{'（' + c['layer'] + '）' if c.get('layer') else ''}：{c.get('what')}"
        return msg(actor, txt, "claim")
    # ---------------- 2.7 表单阶段、分开审、找茬者、裸题对照 ----------------
    # ---------------- 2.9 插话与暂停 ----------------
    if t == "用户指令":
        m = msg("用户", content.get("内容", ""))
        m["tag"] = f"补充 {content.get('编号', '')} · {content.get('生效', '')}起所有角色都会收到"
        return m
    if t == "已暂停":
        return msg("程序", "已暂停：" + str(content.get("说明", "")), "pause")
    if t == "直接打断":  # 2.9.1
        n = len(content.get("作废的调用") or [])
        return msg("用户", "直接打断：正在跑的调用已作废" + (f"（{n} 个）" if n else "") + "，不补做。", "system")
    if t == "转折点":
        return msg("程序", str(content.get("说明", "")) + "。", "system")
    if t == "运行开始":  # 2.10
        return None
    if t == "时间戳":
        return msg("程序", f"时间戳（第 {content.get('第几次')} 次）：{content.get('结果')}" +
                   (f"，{content.get('原因')}" if content.get("原因") else ""), "system")
    if t == "继续运行":
        return msg("程序", "▶ 继续运行：" + str(content.get("说明", "")), "system")
    if t == "插话未送达":
        return msg("程序", "这几条话没有送达：" + "；".join(content.get("内容") or []) + "。" + str(content.get("说明", "")), "warn")
    if t == "表单阶段设置":  # 2.8
        return msg("程序", ("本项目开启表单阶段：" if content.get("开启") else "本项目不拆表单：") + str(content.get("说明", "")), "system")
    if t == "表单提交":
        rd = content.get("round", 1)
        f = content.get("form") or {}
        if content.get("carried"):
            return msg(actor, f"{content.get('item')}（表单第 {rd} 轮）：没有收到针对我的意见，表单沿用上一轮", "quiet")
        lines = [f"{content.get('item')}（表单第 {rd} 轮{'' if rd == 1 else '，修改后'}）：拆成 {len(f.get('items') or [])} 项，"
                 f"术语展开 {len(f.get('terms') or [])} 条，隐含项 {len(f.get('implicit') or [])} 条"]
        for i, it in enumerate(f.get("items") or [], 1):
            lines.append(f"{i}.〔{it.get('field')}〕{it.get('text')}")
        for x in f.get("terms") or []:
            lines.append(f"术语：{x.get('term')}（{x.get('range')}）")
        for x in f.get("implicit") or []:
            lines.append(f"隐含项〔{x.get('field')}〕{x.get('content')}（理由：{x.get('reason')}）")
        for h in (f.get("check") or {}).get("hints") or []:
            lines.append(f"（程序提示：{h}）")
        return msg(actor, "\n".join(lines), "claim")
    if t == "表单评审":
        ov = "；".join(f"{k}：{v}" for k, v in (content.get("总体评价") or {}).items())
        return msg(actor, f"{content.get('item')}（表单第 {content.get('round')} 轮审查）：{ov}；意见 {content.get('意见数')} 条；"
                          f"术语展开一致：{content.get('术语展开一致')}；隐含项一致：{content.get('隐含项一致')}", "claim")
    if t in ("表单意见",):
        spk = "程序" if content.get("来源") == "程序" else actor
        return msg(spk, f"表单意见 {ref}（给 {who(content.get('对象', ''), names)}）｜{content.get('严重程度')}"
                        + (f"（{content['被反驳的依据']}）" if content.get("被反驳的依据") else "")
                        + f"：{content.get('发出的意见')}", "claim")
    if t == "表单归类差异":
        return msg("程序", f"{content.get('item')} 表单第 {content.get('round')} 轮：各份表单有 {len(content.get('差异') or [])} 处归类不同，"
                           "已发给相关生产者核实", "system")
    if t == "表单小结":
        return msg("程序", f"{content.get('item')} 表单第 {content.get('round')} 轮小结：硬伤 {content.get('硬伤意见数')} 条，"
                           f"归类差异 {content.get('归类差异数')} 处" + ("；一致" if content.get("闭合") else "；未一致"), "system")
    if t == "表单候选":
        st_ = content.get("状态")
        return msg("程序", f"{ref} 表单阶段结束：{st_}" + (f"（{content.get('原因')}）" if content.get("原因") else "")
                           + f"，共 {content.get('轮数')} 轮。", "system" if st_ == "闭合" else "warn")
    if t == "等待确认表单":
        return msg("程序", "表单阶段已结束，等你确认：点下方「确认表单」逐题查看，可以直接改、可以撤销；确认后点「继续运行」开始做题。"
                           f"待确认：{'、'.join(content.get('题号') or [])}", "alert")
    if t == "表单冻结":
        return msg("用户" if actor == "用户" else "程序", f"{ref} 的表单已冻结（{content.get('来源')}，第 {content.get('版本')} 版，"
                                                         f"改动 {len(content.get('用户改动') or [])} 处）。之后做题只依据这张表单。", "system")
    if t == "表单不能自动放行":
        return msg("程序", f"{ref} 的表单不能自动放行：{content.get('原因')}，等你确认。", "warn")
    if t == "表单自动放行":
        return None
    if t == "找茬":
        iss = content.get("问题") or []
        txt = f"{content.get('item')}（{content.get('阶段')}第 {content.get('round')} 轮找茬，只交审查者核实）：" + (f"找到 {len(iss)} 条" if iss else "没有发现问题")
        for x in iss:
            txt += f"\n〔{x.get('type')}｜{'、'.join(x.get('targets') or [])}〕{x.get('content')}"
        return msg(actor, txt, "claim")
    if t == "复查":
        ds = content.get("结论") or []
        a = sum(d.get("decision") == "采纳" for d in ds)
        return msg(actor, f"{content.get('item')}（{content.get('阶段')}第 {content.get('round')} 轮）复查找茬者的问题：采纳 {a} 条、驳回 {len(ds) - a} 条", "normal")
    if t == "逐份评审":
        rq = content.get("逐条结论") or []
        cnt = {k: sum(x.get("verdict") == k for x in rq) for k in ("满足", "不满足", "无法判断")}
        txt = (f"{content.get('item')}（第 {content.get('round')} 轮，单独审答卷{content.get('对象')}）：{content.get('总体评价')}；"
               f"逐条：满足 {cnt['满足']}、不满足 {cnt['不满足']}、无法判断 {cnt['无法判断']}")
        for e2 in content.get("额外前提") or []:
            txt += f"\n额外前提：「{e2.get('quote')}」" + (f"（与第{e2['conflict_req']}条冲突）" if e2.get("conflict_req") else "")
        if content.get("超出口径（按次要处理）"):
            txt += "\n（程序注：有意见引用的是排除项，超出口径，按次要处理）"
        if content.get("审查者越界（自己填了答案）"):
            txt += f"\n⚠ 审查者越界：自己填了答案「{content['审查者越界（自己填了答案）']}」（不采用）"
        return msg(actor, txt, "claim")
    if t == "对照审查":
        cs = content.get("材料间冲突") or []
        txt = f"{content.get('item')}（第 {content.get('round')} 轮对照）：材料间冲突 {len(cs)} 条；路线相同：{'是' if content.get('路线相同') else '否'}"
        for c2 in cs:
            txt += f"\n〔{c2.get('severity')}〕{c2.get('a')}「{c2.get('quote_a')}」 vs {c2.get('b')}「{c2.get('quote_b')}」：{c2.get('explain')}"
        return msg(actor, txt, "claim")
    if t == "裸题对照":
        return msg(actor, f"裸题对照 {content.get('item')}：{content.get('answer') or content.get('说明')}", "quiet")
    if t == "错误归因":
        return msg("用户", f"错误归因 {ref}：{content.get('归因')}" + (f"（{content.get('note')}）" if content.get("note") else ""), "system")
    if t == "意见回应":
        return msg(actor, f"对意见 {content.get('opinion')}：{content.get('decision')}。核实：{content.get('check')}", "normal")
    if t == "代码运行":
        return msg("程序", f"{content.get('item')} 第 {content.get('round')} 轮：运行 {who(content.get('initiator', ''), names)} 写的"
                           f"{content.get('类型')}代码 → {content.get('状态')}（{content.get('用时')} 秒）", "quiet")
    if t == "评审":
        ov = "；".join(f"{k}：{v}" for k, v in (content.get("总体评价") or {}).items())
        txt = f"{content.get('item')}（第 {content.get('round')} 轮审查，不做题只比对）：{ov}；意见 {content.get('意见数')} 条"
        for v in content.get("独立验证") or []:
            txt += (f"\n独立验证 {v.get('target')}：{v.get('result')}"
                    + (f"（{v.get('diagnosis')}）" if v.get("diagnosis") not in (None, "无") else "")
                    + (f"｜核对类型：{v.get('check_type')}" + (f"（{v.get('check_note')}）" if v.get("check_note") else "")
                       if v.get("check_type") not in (None, "无") else "") + f" {v.get('reason') or ''}")
        if content.get("未实测或不确定项"):
            txt += "\n未实测/不确定：" + "；".join(content["未实测或不确定项"])
        for u in content.get("未验证的部分") or []:
            txt += f"\n未验证的部分（不挡闭合）{u.get('target')}：{u.get('what')}　试过：{u.get('tried')}"
        return msg(actor, txt, "claim")
    if t == "评审意见":
        return msg(actor, f"意见 {ref}（给 {who(content.get('对象', ''), names)}）｜{content.get('严重程度')}{('（被反驳：' + content['被反驳的依据'] + '）') if content.get('被反驳的依据') else ''}｜{(content.get('层') + '｜') if content.get('层') else ''}{content.get('位置')}：{content.get('发出的意见')}", "claim")
    if t == "轮次小结":
        return msg("程序", f"{content.get('item')} 第 {content.get('round')} 轮小结：答案比对{content.get('答案比对')}，硬伤意见 {content.get('硬伤意见数')} 条"
                           + ("；闭合" if content.get("闭合") else "；未闭合"), "system")
    if t == "最终状态":
        if content.get("状态") == "闭合":
            tip = "；" + "；".join(content.get("独立性提示") or []) if content.get("独立性提示") else ""
            return msg("程序", f"{ref}：闭合（{content.get('一致方式')}，第 {content.get('轮数')} 轮）→ {content.get('共识')}{tip}", "system")
        st_ = content.get("状态")
        if st_ in ("未得出", "部分得出"):
            return msg("程序", f"{ref}：{st_}（各方都没有得出完整答案，审查者认可理由，第 {content.get('轮数')} 轮）：{content.get('未得出或部分得出者')}", "system")
        if st_ == "仅一方得出":
            return msg("程序", f"{ref}：仅一方得出（{content.get('原因')}），没有独立的第二方，不计为对，请你裁决。各生产者最终状态：{content.get('最终')}", "warn")
        return msg("程序", f"{ref}：有争议（{content.get('原因')}），不计为对，请你裁决。各生产者最终答案：{content.get('最终')}", "warn")
    if t == "对照标准答案":
        return msg("程序", f"{ref} 对照标准答案：{content.get('result')}", "quiet")
    if t == "人工判断":
        return msg("用户", f"人工判断 {ref}：{content.get('verdict')}", "system")
    if t == "续写":  # 2.11
        return msg("程序", f"{call_label(content.get('call_key', ''))}：思考末尾疑似循环，已掐断。把重复开始之前的 "
                          f"{content.get('交还的思考字数', 0):,} 字思考交还同一个模型，只要求按原格式写出回答"
                          f"（思考：{content.get('思考', '')}）；不成再从头重试一次。", "warn")
    if t == "跳过续写":  # 2.11
        return msg("程序", f"不续写：{content.get('原因', '')}。", "warn")
    if t == "模型调用":
        if content.get("status") == "成功":
            m = msg(actor, _names_in(_purpose(ref or ""), names), "quiet")
            if content.get("续写"):  # 2.11
                m["text"] += "（这份回答是根据被掐断前的思考续写的）"
                m["kind"] = "warn"
            if content.get("疑似循环（未掐断）"):
                m["text"] += "（疑似循环，未掐断：" + "、".join(
                    f"{w.get('rule')}（{w.get('channel')}）" for w in content["疑似循环（未掐断）"]) + "）"
        else:
            m = msg(actor, f"调用没成功：{content.get('failure') or '未知原因'}（程序会自动重试）", "warn")
        if content.get("reasoning_chars"):  # 2.5：这次调用有思考过程 → 界面可以挂一个折叠的思考框
            m["think"] = {"call": ref, "secs": content.get("think_seconds"), "chars": content.get("reasoning_chars"),
                          "total_secs": content.get("seconds")}
        return m
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
    if t == "调用失败":
        role = content.get("role", "")
        others = content.get("仍在运行") or []
        tail = ("；" + "、".join(who(o, names) for o in others) + " 还在运行，等它们完成本步后再暂停（不会中途取消，已经花的钱不浪费）"
                ) if others else "；本步没有其他角色在运行，即将暂停"
        return msg("程序", f"{who(role, names)}（{role}）没能拿到回应：{content.get('reason') or '未知原因'}{tail}", "warn")
    if t == "调用失败暂停":
        role = content.get("role", "")
        reason = content.get("reason") or "未知原因"
        m = msg("程序", f"{who(role, names)}（{role}）连续没能拿到回应，已暂停：{reason}\n"
                        f"处理好以后点「继续运行」，已完成的部分不会重复花钱。", "warn")
        m["popup"] = {"role": role, "reason": reason}  # 2.0 界面待办：前端据此弹窗，不能只提示"调用失败"
        return m
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
    if t == "参数变更":  # 2.6
        parts = "；".join(f"{k}：{v.get('旧')} → {v.get('新')}" for k, v in (content or {}).items())
        return msg("用户", f"参数和上次运行时不同，已记入账本：{parts}", "system")
    if t == "实际模型不一致":  # 2.6
        return msg("程序", f"{who(content.get('role',''), names)}：请求的模型是 {content.get('请求')}，接口实际返回的是 "
                          f"{content.get('实际')}（接口方可能把旧模型名转到了新模型）。", "warn")
    return msg(actor if actor in names else "程序", f"{t}", "quiet")
