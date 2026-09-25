"""迭代流程（第 6 节每轮 10 步），第一期实现范围：
- 生产者 2 个及以上、审查者 1 个及以上；
- 找茬者、记录者尚未实现，对应步骤只记事件；
- 准入门由用户手动放行（创建项目即视为放行，入账"准入：用户手动放行"）。
"""

from __future__ import annotations

import json

import re
from urllib.parse import urlparse

from . import blocks as B
from .config import memory_of
from .models import CallFailedPause
from .textnorm import clean_md, find_skeleton
from . import tasks as TASKS
from .tasks import fact_compilation as _FC
from .util import FormatError, canonical_json, find_numbers, normalize_ws, sha256_text


class Flow:
    def __init__(self, project, settings, hub, retriever):
        self.p, self.s, self.hub, self.ret = project, settings, hub, retriever
        self.cfg = project.config
        self.items = self.cfg["items"]
        self.item_ids = {it["id"] for it in self.items}
        self.roles = settings["roles"]
        self.producers = [r for r, v in self.roles.items() if v["kind"] == "生产者"]
        self.reviewers = [r for r, v in self.roles.items() if v["kind"] == "审查者"]
        if len(self.producers) < 2 or len(self.reviewers) < 1:
            raise ValueError("第一期至少需要 2 个生产者、1 个审查者")
        self._novelty = {}
        self.T = TASKS.load(self.cfg)  # 1.8：题目包按项目选用
        if self.cfg.get("params"):  # 1.8：问题文件可以覆盖个别流程参数（如每次调用处理几个条目）
            self.s = dict(settings, params={**settings["params"], **self.cfg["params"]})
        self.F = self.T.FIELD  # 1.7：主字段的程序核对器（字段类型由题目包声明）
        self.trusted = list(self.cfg.get("trusted_sites") or settings.get("trusted_sites") or [])
        self.mem = {r: memory_of(v) for r, v in self.roles.items()}
        self._init_items()

    # ---------------- 基础 ----------------
    def _init_items(self):
        for it in self.items:
            if not self.p.q("SELECT 1 FROM items WHERE item_id=?", (it["id"],)):
                self.p.exec("INSERT INTO items (item_id,name,type,st_check,st_verify,st_indep,st_valid,history)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (it["id"], it["name"], "条目", "未查证", "未验证", None, "有效", "[]"))

    def series(self, role):
        return self.roles[role].get("series", role)

    def completed_rounds(self):
        rows = self.p.q("SELECT round FROM events WHERE type='轮次完成' ORDER BY seq")
        return [r["round"] for r in rows]

    def is_finished(self):
        return bool(self.p.q("SELECT 1 FROM events WHERE type='迭代结束'"))

    def group_hint(self, iid):
        g = next((it.get("group") for it in self.items if it["id"] == iid), None) or ""
        return g.rstrip("朝") or None

    def latest_block(self, role, iid, upto):
        """该角色在该条目上最近一轮（不晚于 upto）的有效块。1.4 起已定论的条目不再重写，沿用最近的块。"""
        r = self.p.q("SELECT * FROM blocks WHERE author=? AND item_id=? AND round<=? AND st_valid='有效' "
                     "ORDER BY round DESC LIMIT 1", (role, iid, upto))
        return dict(r[0]) if r else None

    def settled(self, iid):
        row = self.p.q("SELECT history FROM items WHERE item_id=?", (iid,))[0]
        hist = json.loads(row["history"] or "[]")
        return bool(hist and hist[-1].get("终态"))

    # ---------------- 记忆模式（1.4） ----------------
    def visible_roles(self, role):
        """该角色能看到哪些生产者的历史结果（含自己）。"""
        m = self.mem[role]
        if m["mode"] == "全局":
            return set(self.producers)
        if m["mode"] == "全新":
            return set()
        return {role if x == "self" else x for x in m.get("see", ["self"])} & set(self.producers)

    def memory_rounds(self, role, r):
        m = self.mem[role]
        if m["mode"] == "全局":
            return list(range(1, r))
        if m["mode"] == "全新":
            return []
        n = max(1, int(m.get("rounds", 1)))
        return list(range(max(1, r - n), r))

    def history_for(self, role, r, items):
        """按记忆模式给出 (自己之前的块, 他方之前的块)。"""
        rounds, see = self.memory_rounds(role, r), self.visible_roles(role)
        ids = {it["id"] for it in items}

        def blocks(x):
            out = []
            for rr in rounds:
                out += [dict(round=rr, item=b["item_id"], block=b["block_id"], **json.loads(b["fields"]))
                        for b in self.blocks_of(x, rr) if b["item_id"] in ids]
            return out
        own = blocks(role) if role in see else []
        others = {x: blocks(x) for x in sorted(see - {role})}
        others = {x: v for x, v in others.items() if v}
        return own or None, others or None

    def opinions_for(self, role):
        """针对该角色的未闭合意见；隔离模式下给的是不含他方数值与出处的版本。"""
        ops = self.open_opinions(role)
        iso = self.mem[role]["mode"] != "全局"
        for o in ops:
            o["shown"] = (o.get("content_isolated") or o["content"]) if iso else o["content"]
        return ops

    def mask_foreign(self, text, target, iid):
        """把意见里出现的、只属于其他生产者的年份与检索编号遮掉（程序机械处理）。"""
        mine = ""
        b = self.latest_block(target, iid, 10 ** 6)
        if b:
            mine = b["fields"]
        own_ids = {x["retrieval_id"] for x in self.p.q("SELECT retrieval_id FROM retrievals WHERE requester=?",
                                                        (target,))}
        foreign_years = set()
        for x in self.producers:
            if x == target:
                continue
            ob = self.latest_block(x, iid, 10 ** 6)
            if ob:
                foreign_years |= set(re.findall(r"(?<!\d)\d{3,4}(?!\d)", ob["fields"]))
        foreign_years -= set(re.findall(r"(?<!\d)\d{3,4}(?!\d)", mine))
        t = re.sub(r"S\d{5}", lambda m: m.group(0) if m.group(0) in own_ids else "（他方来源）", text or "")
        for y in sorted(foreign_years, key=len, reverse=True):
            t = re.sub(rf"(?<!\d){y}(?!\d)", "某", t)
        return t

    def blocks_of(self, role, round_):
        rows = self.p.q("SELECT * FROM blocks WHERE author=? AND round=? AND st_valid='有效'", (role, round_))
        return [dict(r) for r in rows]

    def open_opinions(self, role):
        rows = self.p.q("SELECT * FROM opinions WHERE target_author=? AND status IN ('提出','核验中')", (role,))
        return [dict(r) for r in rows]

    def artifact_of(self, role, round_):
        r = self.p.q("SELECT * FROM artifacts WHERE author=? AND round=? AND type='方案' AND status='有效'",
                     (role, round_))
        return dict(r[0]) if r else None

    def _discard_incomplete(self, r):
        """未完成的轮次重做：清掉该轮的表记录后按同样的编号重建（账本事件保留，只追加，
        并记一条"重做未完成轮次"事件；已成功的模型调用与检索直接复用存档，不重复发出）。"""
        n = self.p.q("SELECT COUNT(*) c FROM blocks WHERE round=?", (r,))[0]["c"]
        if n:
            self.p.exec("UPDATE opinions SET status='作废' WHERE review_id IN "
                        "(SELECT review_id FROM reviews WHERE round=?)", (r,))
            self.p.exec("DELETE FROM reviews WHERE round=?", (r,))
            self.p.exec("DELETE FROM blocks WHERE round=?", (r,))
            self.p.exec("UPDATE artifacts SET status='作废' WHERE round=?", (r,))
            self.p.log("程序", "重做未完成轮次", round_=r, content={"清除块数": n})

    # ---------------- 主循环 ----------------
    def run(self):
        if not self.p.q("SELECT 1 FROM events WHERE type='准入判断'"):
            self.p.log("用户", "准入判断", content={"结果": "强制放行", "说明": "第一期未实现准入门，由用户手动放行"})
        if self.is_finished():
            return "已结束"
        done = self.completed_rounds()
        r = (max(done) + 1) if done else 1
        max_rounds = self.s["params"]["max_rounds"]
        try:
            while r <= max_rounds:
                self._discard_incomplete(r)
                finished = self.run_round(r)
                self.p.log("程序", "轮次完成", round_=r)
                if finished:
                    self.p.log("程序", "迭代结束", round_=r, content={"原因": "全部条目已有终态且无未闭合意见"})
                    return "完成"
                r += 1
            self.p.log("程序", "迭代结束", round_=r - 1, content={"原因": f"达到迭代轮次上限 {max_rounds}"})
            return "达到上限"
        except CallFailedPause as e:
            return f"调用失败暂停：{e}。检查问题后重新运行即可从失败处继续。"

    # ---------------- 一轮 ----------------
    def run_round(self, r):
        self._novelty = {}
        # 第 1 步 分发
        delivered = {}
        for p in self.producers:
            ins = [a["output_hash"] for a in (self.artifact_of(x, r - 1) for x in self.producers) if a]
            ins += [row["review_id"] for row in self.p.q("SELECT review_id FROM reviews WHERE round=?", (r - 1,))]
            delivered[p] = ins
            self.p.log("程序", "分发", round_=r, ref_type="role", ref_id=p, content={"输入": ins})
        # 第 8.9 条配置一致性检查（第 2 轮起）
        if r >= 2:
            for p in self.producers:
                authors = {a["author"] for a in self.p.q(
                    "SELECT author FROM artifacts WHERE round=? AND status='有效'", (r - 1,))} | set(self.reviewers)
                if not (authors - {p}):
                    self.p.log("程序", "配置一致性提示", round_=r, ref_id=p)

        # 第 2、3 步 生产与提交检查
        for p in self.producers:
            self.produce(p, r, delivered[p])

        # 第 4 步 对照
        table = {}
        for it in self.items:
            table[it["id"]] = {p: (self.F.value(json.loads(b["fields"])) if b else None)
                               for p in self.producers for b in [self.latest_block(p, it["id"], r)]}
        path, h = self.p.save_artifact_file("程序", r, "对照表", json.dumps(table, ensure_ascii=False, indent=1))
        self.p.log("程序", "对照", round_=r, ref_type="file", ref_id=path, content={"hash": h})

        # 第 5、6 步 审查分发与审查（审查矩阵）
        for rv in self.reviewers:
            for p in self.producers:
                self.review(rv, p, r)
        self.apply_reviews(r)

        # 第 7—9 步
        self.p.log("程序", "找茬者未出场", round_=r, content={"说明": "第一期未实现找茬者"})
        self.p.log("程序", "回流登记", round_=r)
        self.p.log("程序", "摘要", round_=r, content={"说明": "第一期未设记录者；本轮摘要见轮次统计"})

        # 第 10 步 状态更新
        return self.update_state(r)

    # ---------------- 生产 ----------------
    def produce(self, p, r, delivered):
        todo = [it for it in self.items if not self.settled(it["id"])]  # 已定论的条目不再重写
        if not todo:
            self._novelty[p] = (0, 0.0)
            return
        todo_ids = {it["id"] for it in todo}
        own_prev, others = self.history_for(p, r, todo)
        ops = [o for o in self.opinions_for(p) if o["item_id"] in todo_ids]
        maxq = self.s["params"]["search_results_per_query"]

        # 检索阶段（第一期每轮检索一次；内层多次检索属第二期）。检索词同样受记忆模式约束。
        def val_search(o):
            if not isinstance(o.get("searches"), list):
                raise FormatError("缺少 searches 数组")
            for q in o["searches"]:
                if not isinstance(q, dict):
                    raise FormatError("searches 的每一项须是 {item, query} 对象")
                if q.get("item") not in todo_ids or not q.get("query"):
                    raise FormatError(f"检索项不合规：{q}（item 只能是本轮清单里的条目编号）")

        sq = self.hub.call_json(p, self.T.SYSTEM_PRODUCER, self.T.search_prompt(self.cfg, todo, own_prev, ops, others),
                                call_key=f"R{r}-{p}-search", round_=r, validator=val_search)
        for q in sq["searches"][: 2 * len(todo)]:
            prior = self.p.q("SELECT retrieval_id FROM retrievals WHERE requester=? AND round=? AND query=?",
                             (p, r, q["query"]))
            if not prior:  # 恢复运行时不重复检索
                self.ret.search(p, r, q["query"], maxq, item_id=q["item"])
        # 证据：只用本方自己检索到的原文；按"是否讲到出生"挑来源，不再只取最新几条
        rounds = set(self.memory_rounds(p, r)) | {r}
        evidence = {}
        for it in todo:
            past = [x["retrieval_id"] for x in self.p.q(
                "SELECT retrieval_id, round FROM retrievals WHERE requester=? AND item_id=? ORDER BY retrieval_id",
                (p, it["id"])) if x["round"] in rounds]
            if past:
                evidence[it["id"]] = self.T.excerpts_for(self.p, past, it, self.s["params"]["max_sources_shown"])
        items_all = self.items
        self.items = todo  # 本轮只写未定论的条目
        try:
            self._write_and_check(p, r, delivered, evidence, own_prev, others, ops)
        finally:
            self.items = items_all

    def _write_and_check(self, p, r, delivered, evidence, own_prev, others, ops):
        # 撰写阶段
        def val_product(o, bids):
            # 1.9：一批里交对的条目先收下——不属于本批的、重复的块去掉；缺的条目记为本轮没交，下一轮再补
            if isinstance(o.get("blocks"), list):
                seen, keep, dropped = set(), [], 0
                for b in o["blocks"]:
                    it = b.get("item") if isinstance(b, dict) else None
                    if it in bids and it not in seen:
                        seen.add(it)
                        keep.append(b)
                    else:
                        dropped += 1
                o["blocks"] = keep
                o["_缺交"] = sorted(bids - seen)
                o["_去掉"] = dropped
            B.validate_product(o, block_schema=self.T.BLOCK_SCHEMA, item_ids=bids, require_changes=r > 1)
            for b in o["blocks"]:
                st = b["fields"].get("状态")
                if st not in self.T.ENTRY_STATES:
                    raise FormatError(f"条目 {b['item']} 的状态 {st!r} 不合规，只能是 {self.T.ENTRY_STATES}")
                if st != "资料缺载" and not b["fields"].get("出处"):
                    raise FormatError(f"条目 {b['item']} 状态为 {st} 时须填出处")
            if not o["blocks"]:
                raise FormatError(f"本批没有一个有效的条目块；应为：{sorted(bids)}")

        # 分批撰写，控制单次输入长度（每批 items_per_call 个条目）
        size = self.s["params"]["items_per_call"]
        prod = {"blocks": [], "notes": "", "changes": [], "responses": []}
        for k in range(0, len(self.items), size):
            batch = self.items[k:k + size]
            bids = {it["id"] for it in batch}
            ev_b = {i: e for i, e in evidence.items() if i in bids}
            prev_b = [x for x in own_prev if x["item"] in bids] if own_prev else None
            oth_b = {x: [y for y in v if y["item"] in bids] for x, v in others.items()} if others else None
            ops_b = [o for o in ops if o["item_id"] in bids]
            part = self.hub.call_json(p, self.T.SYSTEM_PRODUCER,
                                      self.T.write_prompt(self.cfg, batch, ev_b, r, prev_b, oth_b, ops_b),
                                      call_key=f"R{r}-{p}-write-{k // size + 1}", round_=r,
                                      validator=lambda o, bids=bids: val_product(o, bids))
            for b in part["blocks"]:  # 字段值统一转为文本
                b["fields"] = {k: ("" if v is None else str(v)) for k, v in b["fields"].items()}
            prod["blocks"] += part["blocks"]
            if part.get("_缺交") or part.get("_去掉"):
                self.p.log("程序", "部分提交", round_=r, ref_type="role", ref_id=p,
                           content={"本轮没交的条目": part.get("_缺交") or [], "去掉的多余或重复块": part.get("_去掉") or 0})
            prod["notes"] += (part.get("notes") or "")
            prod["changes"] += part.get("changes") or []
            prod["responses"] += part.get("responses") or []

        # 保存产物（程序命名）与三个哈希
        text = json.dumps(prod, ensure_ascii=False, indent=1)
        path, h = self.p.save_artifact_file(p, r, "方案", text)
        prev_art = self.artifact_of(p, r - 1)
        aid = self.p.next_id("artifacts", "A", "artifact_id")
        ev = self.p.log(p, "提交产物", round_=r, ref_type="artifact", ref_id=aid,
                        content={"file": path, "output_hash": h})
        self.p.exec("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (aid, p, r, "方案", path, h, canonical_json(delivered),
                     prev_art["output_hash"] if prev_art else "无", ev, "有效"))

        # 提交检查（第 3 步）：块编号、出处（须为本方检索）、原句、农历换算、数字
        prev_fields = {it["id"]: (lambda b: b["fields"] if b else None)(self.latest_block(p, it["id"], r - 1))
                       for it in self.items}
        changed = 0
        n = 0
        for b in prod["blocks"]:
            n += 1
            bid = f"{p}-R{r}-B{n:03d}"
            f = b["fields"]
            flags, st_check = self.check_block(p, b["item"], f)
            flags += B.flag_notes(prod.get("notes", "")) if n == 1 else []
            if prev_fields.get(b["item"]) != canonical_json({k: v for k, v in f.items() if not k.startswith("_")}):
                changed += 1
            stored = {k: v for k, v in f.items() if not k.startswith("_")}
            self.p.exec("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (bid, aid, p, r, b["type"], b["item"], canonical_json(stored), st_check, "未验证",
                         None, "有效", "[]", json.dumps(flags, ensure_ascii=False)))
            self.p.log("程序", "提交检查", round_=r, ref_type="block", ref_id=bid,
                       content={"查证": st_check, "标记": flags})

        # 意见处理（采纳门）
        for resp in prod.get("responses", []) or []:
            op = self.p.q("SELECT * FROM opinions WHERE opinion_id=? AND target_author=?", (resp.get("opinion"), p))
            ev_ok = self.p.q("SELECT 1 FROM retrievals WHERE retrieval_id=? AND item_id=? AND requester=?",
                             (resp.get("evidence"), op[0]["item_id"], p)) if op else []
            if op and op[0]["status"] in ("提出", "核验中") and ev_ok:
                self.p.exec("UPDATE opinions SET status=?, closed_round=?, evidence=? WHERE opinion_id=?",
                            (resp["action"], r, resp["evidence"], resp["opinion"]))
                self.p.log(p, "意见处理", round_=r, ref_type="opinion", ref_id=resp["opinion"],
                           content={"处理": resp["action"], "核验记录": resp["evidence"], "理由": resp.get("reason")})
            else:
                self.p.log("程序", "拒收", round_=r, ref_type="opinion", ref_id=resp.get("opinion"),
                           content={"原因": "意见处理未附本方在该条目下的有效检索编号，意见保持未闭合"})
        total = len(prod["blocks"]) or 1  # 1.6：只按本轮实际重写的条目算（已定论的不再重写）
        self._novelty[p] = (changed, changed / total)

    def check_block(self, p, iid, f):
        """程序能核对的都由程序核对（1.4）。返回 (标记列表, 查证状态)；会就地补上 程序换算 等字段。"""
        flags = []
        src = str(f.get("出处", "")).strip()
        if f.get("状态") == "资料缺载":
            return ["生产者称资料缺载"], "未查证"
        if src == "模型记忆":
            return ["来源为模型记忆，标未查证"], "未查证"
        row = self.p.q("SELECT * FROM retrievals WHERE retrieval_id=?", (src,))
        if not row:
            return [f"出处 {src} 不是已登记的检索编号"], "出处无法解析"
        row = row[0]
        if row["requester"] != p:
            return [f"出处 {src} 不是你自己检索到的，只能引用本方检索的原文"], "未查证"
        if row["source"] not in ("原文", "接口提取正文"):
            return ["出处只有检索片段，不是原文，不能作为引文来源"], "未查证"
        raw = self.p.read_archive(src)
        found, offset, para, ctx = B.locate_quote(raw, f.get("原句", ""))
        if not found:
            return ["原句未在所注检索的存档原文中找到"], "未查证"
        f["_位置"] = offset
        return self.F.check(f, raw, self.group_hint(iid))  # 1.7：字段内容由对应的核对器核对

    # ---------------- 审查 ----------------
    def _host(self, url):
        return urlparse(url or "").netloc.lower()

    def is_trusted(self, url):
        h = self._host(url)
        return any(h == t or h.endswith("." + t) for t in self.trusted)

    def independent_sources(self, rv, r, item, cited):
        """独立来源（1.4）：可信网站上、生产者没有引用的原文。先找已有存档；没有就由程序在可信网站内检索，
        检索词由程序生成（人名 + 出生），不含任何日期数字。返回 [{编号, 网址, 段落}]。"""
        if not self.trusted:
            return []
        keys = self.T.aliases(item)
        cited_rows = self.p.q(f"SELECT content_hash, url FROM retrievals WHERE retrieval_id IN ({','.join('?' * len(cited))})",
                              tuple(cited)) if cited else []
        cited_hash = {x["content_hash"] for x in cited_rows}
        cited_host = {self._host(x["url"]) for x in cited_rows}  # 独立来源须来自生产者引用的网站以外的可信网站

        def pool():
            out = []
            for row in self.p.q("SELECT retrieval_id,url,content_hash,source FROM retrievals WHERE item_id=? "
                                "ORDER BY retrieval_id", (item["id"],)):
                if row["retrieval_id"] in cited or row["content_hash"] in cited_hash or self._host(row["url"]) in cited_host:
                    continue
                if row["source"] not in ("原文", "接口提取正文") or not self.is_trusted(row["url"]):
                    continue
                wins = self.T.windows(clean_md(self.p.read_archive(row["retrieval_id"]) or ""), keys, top=1)
                if wins and wins[0][0] >= 5:  # 须同时有"出生"一类说法和日期，才当作独立来源
                    out.append((wins[0][0], row["retrieval_id"], row["url"], wins[0][1]))
            out.sort(key=lambda x: (-x[0], x[1]))
            seen, res = set(), []
            for sc, rid, url, w in out:  # 同一网站只取一个
                if self._host(url) in seen:
                    continue
                seen.add(self._host(url))
                res.append({"编号": rid, "网址": url, "段落": w})
            return res[: self.s["params"]["independent_sources"]]

        got = pool()
        base = re.sub(r"[（(][^）)]*[）)]", "", item["name"]).strip()
        query = self.T.independent_query(item) if hasattr(self.T, "independent_query") else f"{base} {self.F.noun}"
        # 1.9：只在生产者没引用过的可信网站里找（否则前几条结果常常全是已引用的网站，找不到独立来源）
        domains = [t for t in self.trusted if not any(h == t or h.endswith("." + t) for h in cited_host)]
        if not got and domains and not self.p.q("SELECT 1 FROM retrievals WHERE requester=? AND item_id=? AND query=?",
                                                (rv, item["id"], query)):
            self.ret.search(rv, r, query, self.s["params"]["search_results_per_query"], item_id=item["id"],
                            include_domains=domains)
            got = pool()
        return got

    def _equivalents(self, iid, date):
        return self.F.equivalents(date, self.group_hint(iid))

    def _birth_date_jds(self, text, iid):
        return self.F.evidence(text, self.group_hint(iid))

    def _same_day(self, date, jds):
        return self.F.matches(date, jds)

    def _cited(self, iid, r):
        """本轮各生产者在该条目上引用的检索（独立来源须避开所有生产者引用过的原文）。"""
        out = []
        for x in self.producers:
            b = self.latest_block(x, iid, r)
            if b:
                src = json.loads(b["fields"]).get("出处")
                if src and src != "模型记忆":
                    out.append(src)
        return out

    def review(self, rv, p, r):
        bl = [b for b in self.blocks_of(p, r) if b["st_check"] == "待审查"]
        if not bl:
            return
        view = []
        glob = self.mem[rv]["mode"] == "全局"
        for b in bl:
            f = json.loads(b["fields"])
            _, _, para, ctx = B.locate_quote(self.p.read_archive(f["出处"]), f.get("原句", ""))
            url = self.p.q("SELECT url FROM retrievals WHERE retrieval_id=?", (f["出处"],))[0]["url"]
            item = next(it for it in self.items if it["id"] == b["item_id"])
            v = {"block": b["block_id"], "item": b["item_id"], "人物": f.get("人物"),
                 **self.F.review_values(f),
                 "原句": f.get("原句"), "出处": f["出处"], "来源网址": url, "原文前后文": ctx or para,
                 self.F.equivalents_label: self._equivalents(b["item_id"], self.F.value(f)),
                 "独立来源": self.independent_sources(rv, r, item, self._cited(b["item_id"], r)) or "（无）"}
            if glob:
                v["其他生产者本轮所填（仅供参考，不作为判断依据）"] = {
                    x: self.F.value(json.loads(ob["fields"])) for x in self.producers if x != p
                    for ob in [self.latest_block(x, b["item_id"], r)] if ob}
            view.append(v)
        size = self.s["params"]["items_per_call"]
        res = {"reviews": []}
        for k in range(0, len(view), size):
            part_v = view[k:k + size]
            ids = [x["block"] for x in part_v]
            part = self.hub.call_json(rv, self.T.SYSTEM_REVIEWER, self.T.review_prompt(self.cfg, p, part_v),
                                      call_key=f"R{r}-{rv}-review-{p}-{k // size + 1}", round_=r,
                                      validator=lambda o, ids=ids: self.T.validate_review(o, ids))
            res["reviews"] += part["reviews"]
        same = int(self.series(rv) == self.series(p))
        for x in res["reviews"]:
            b = next(bb for bb in bl if bb["block_id"] == x["block"])
            eq, ind = x.get("equivalent"), x.get("independent")
            v = next(vv for vv in view if vv["block"] == x["block"])
            srcs = v["独立来源"] if isinstance(v["独立来源"], list) else []
            # 1.6：审查者可能一次写几个编号（如"S00070,S00071"）；只认程序提供给它的独立来源
            offered = {s_["编号"] for s_ in srcs}
            claimed = re.findall(r"S\d{5}", str(x.get("independent_source") or ""))
            valid = [c_ for c_ in dict.fromkeys(claimed) if c_ in offered]
            x["independent_source"] = ",".join(valid) or None
            if ind == "支持" and not valid:
                ind = "无独立来源"
                x["independent"] = ind
                x["reason"] = (x.get("reason") or "") + (
                    f"【程序：所填独立来源 {'、'.join(claimed) or '（空）'} 不是程序提供的可信网站独立来源，"
                    "不能算支持，改判为无独立来源】")
            src = next((s_ for s_ in srcs if s_["编号"] in valid), srcs[0] if srcs else None)
            ind_jds = self._birth_date_jds(src["段落"], b["item_id"]) if src else None
            if ind == "矛盾" and src and any(self._same_day(self.F.value(v), self._birth_date_jds(s_["段落"], b["item_id"]))
                                             for s_ in srcs):
                # 程序换算后是同一天（只是历法或农历/公历写法不同），不算矛盾
                ind = "支持"
                x["independent"] = ind
                x["reason"] = (x.get("reason") or "") + "【程序：独立来源的日期换算后与所填日期是同一天，改判为支持】"
            verdict = self.T.review_verdict(x)
            rid = self.p.next_id("reviews", "V", "review_id")
            oid = None
            if eq == "不等价" or (eq is None and verdict in ("错误", "需补充")):
                oid = self.p.next_id("opinions", "O", "opinion_id")
                full = f"审查者判定原句与所填日期不等价：{x.get('reason', '')}；建议：{x.get('suggestion', '')}"
                self.p.exec("INSERT INTO opinions (opinion_id,source_role,review_id,target_author,item_id,content,"
                            "status,closed_round,evidence,kind,content_isolated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (oid, rv, rid, p, b["item_id"], full, "提出", None, None, "审查",
                             self.mask_foreign(full, p, b["item_id"])))
            self.p.exec("INSERT INTO reviews (review_id,reviewer,artifact_id,author,round,same_model,item_id,block_id,"
                        "verdict,severity,method,evidence,reasoning,changes_state,opinion_id,eq_check,indep_check,"
                        "indep_source,indep_dates) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, rv, b["artifact_id"], p, r, same, b["item_id"], b["block_id"], verdict,
                         x.get("severity"), "等价核对+独立核验", b["block_id"], x.get("reason", ""), 1, oid,
                         eq, ind, x.get("independent_source") or None,
                         json.dumps(sorted(ind_jds)) if ind_jds else None))
            self.p.log(rv, "审查意见", round_=r, ref_type="review", ref_id=rid,
                       content={"块": b["block_id"], "判定": verdict, "等价": eq, "独立核验": ind,
                                "独立来源": x.get("independent_source") or None,
                                "同模型审查": bool(same), "意见": oid})

    def apply_reviews(self, r):
        """全部审查者判完后按块汇总（只看等价核对；独立核验结果在条目层面使用）：
        任一"不等价"→未查证；否则任一"无法判断"→待核验；全部"等价"→已查证（含模型判断）。"""
        for b in self.p.q("SELECT block_id FROM blocks WHERE round=? AND st_check='待审查'", (r,)):
            vs = self.p.q("SELECT verdict, eq_check, same_model FROM reviews WHERE block_id=? AND round=?",
                          (b["block_id"], r))
            if not vs:
                continue
            eqs = {v["eq_check"] or {"正确": "等价", "错误": "不等价", "需补充": "不等价"}.get(v["verdict"], "无法判断")
                   for v in vs}
            if "不等价" in eqs:
                self.p.exec("UPDATE blocks SET st_check='未查证' WHERE block_id=?", (b["block_id"],))
            elif "无法判断" in eqs:
                self.p.exec("UPDATE blocks SET st_verify='待核验' WHERE block_id=?", (b["block_id"],))
            else:
                self.p.exec("UPDATE blocks SET st_check='已查证（含模型判断）' WHERE block_id=?", (b["block_id"],))
                if all(v["same_model"] for v in vs):
                    self.p.exec("UPDATE blocks SET st_indep='仅同模型审查' WHERE block_id=?", (b["block_id"],))

    # ---------------- 状态更新 ----------------
    def _add_opinion(self, r, target, item, kind, content, isolated=None):
        """程序提出的意见按（对象, 条目, 种类）去重。isolated：给隔离模式角色看的版本（不含他方数值与出处）。"""
        dup = self.p.q("SELECT 1 FROM opinions WHERE target_author=? AND item_id=? AND kind=? "
                       "AND status IN ('提出','核验中')", (target, item, kind))
        if dup:
            return
        oid = self.p.next_id("opinions", "O", "opinion_id")
        self.p.exec("INSERT INTO opinions (opinion_id,source_role,review_id,target_author,item_id,content,status,"
                    "closed_round,evidence,kind,content_isolated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, "程序", None, target, item, content, "提出", None, None, kind, isolated))
        self.p.log("程序", "意见提出", round_=r, ref_type="opinion", ref_id=oid,
                   content={"内容": content, "隔离版本": isolated})

    def _close_opinions(self, r, target, item, kinds, why):
        rows = self.p.q("SELECT opinion_id FROM opinions WHERE target_author=? AND item_id=? AND "
                        f"kind IN ({','.join('?' * len(kinds))}) AND status IN ('提出','核验中')",
                        (target, item, *kinds))
        for o in rows:
            self.p.exec("UPDATE opinions SET status='已解决', closed_round=? WHERE opinion_id=?", (r, o["opinion_id"]))
            self.p.log("程序", "意见闭合", round_=r, ref_type="opinion", ref_id=o["opinion_id"], content={"依据": why})

    def _source_key(self, retrieval_id):
        row = self.p.q("SELECT url, content_hash FROM retrievals WHERE retrieval_id=?", (retrieval_id,))[0]
        return self._host(row["url"]), row["content_hash"]

    agree = staticmethod(_FC.FIELD.agree)  # 1.7：保留旧名字（测试用），实际由字段核对器判断

    def _ganzhi(self, iid, blocks, d):
        return self.F.category([json.loads(b["fields"]) for b in blocks], d, self.group_hint(iid))

    def update_state(self, r):
        terminal = True
        last_round = r >= self.s["params"]["max_rounds"]
        for it in self.items:
            iid = it["id"]
            if self.settled(iid):
                continue
            latest = {p: self.latest_block(p, iid, r) for p in self.producers}
            ok = {p: b for p, b in latest.items() if b and b["st_check"].startswith("已查证")}
            for p in ok:
                self._close_opinions(r, p, iid, ["审查", "未通过查证"], f"{ok[p]['block_id']} 已通过查证")
            dates = {p: self.F.normalize(self.F.value(json.loads(b["fields"]))) for p, b in ok.items()}
            dates = {p: d for p, d in dates.items() if d}
            series = {self.series(p) for p in dates}
            states = {p: json.loads(b["fields"])["状态"] for p, b in latest.items() if b}
            merged = self.F.agree(list(dates.values())) if len(dates) >= 2 else None
            if merged:
                d = merged
                cites = [json.loads(ok[p]["fields"])["出处"] for p in dates]
                keys = [self._source_key(c) for c in cites]
                domains, hashes = {k[0] for k in keys}, {k[1] for k in keys}
                blocks = [ok[p] for p in dates]
                inds = [v["indep_check"] for b in blocks for v in self.p.q(
                    "SELECT indep_check FROM reviews WHERE block_id=?", (b["block_id"],))]
                for p in dates:
                    self._close_opinions(r, p, iid, ["分歧"], "各方已查证结果一致")
                gz = self._ganzhi(iid, blocks, d)
                self.p.exec("UPDATE items SET ganzhi=? WHERE item_id=?", (gz, iid))
                yr_note = "（一方只给了年份，与另一方的完整日期同年，视为一致）" if any(self.F.is_partial(x) for x in dates.values()) else ""
                rows = [v for b in blocks for v in self.p.q(
                    "SELECT indep_check, indep_source, indep_dates FROM reviews WHERE block_id=?", (b["block_id"],))]
                sup_ids = {sid for v in rows if v["indep_check"] == "支持" and v["indep_source"]
                           for sid in re.findall(r"S\d{5}", v["indep_source"])}
                sup_hosts = sorted({self._host(row[0]["url"]) for sid in sup_ids
                                    for row in [self.p.q("SELECT url FROM retrievals WHERE retrieval_id=?", (sid,))]
                                    if row})
                cited_txt = "、".join(sorted(domains))
                diff_sites = len(series) >= 2 and len(domains) >= 2 and len(hashes) >= 2
                count = (len(domains) - 1 if diff_sites else 0) + len(sup_hosts)
                indep = "独立" if diff_sites else ("未独立验证" if len(series) < 2 else "独立性未确认")
                if "矛盾" in inds:
                    other = [json.loads(v["indep_dates"]) for v in rows if v["indep_check"] == "矛盾" and v["indep_dates"]]
                    z_ok = gz and other and all(self.F.category_of_evidence(j, self.group_hint(iid)) == gz
                                                for js in other for j in js)
                    self._set_item(r, iid, "已查证（含模型判断）", "待核验", indep, count, self.F.format(d),
                                   f"有争议：两方一致为 {self.F.format(d)}（引用 {cited_txt}），但可信网站的独立来源与之矛盾"
                                   + ("；" + self.T.same_category_note(gz) if z_ok else "") + yr_note, True)
                    if not z_ok:
                        self.p.exec("UPDATE items SET ganzhi=NULL WHERE item_id=?", (iid,))
                elif "支持" in inds:
                    how = ("两方一致、来源网站不同" if diff_sites else f"两方一致（同引 {cited_txt}）") + \
                          f"、原句等价，另由可信网站 {'、'.join(sup_hosts) or '（独立来源）'} 独立支持"
                    if len(series) < 2:
                        how += "；注意：两个生产者属同一系列模型"
                    self._set_item(r, iid, "已查证（含模型判断）", "已验证", indep, count, self.F.format(d),
                                   how + yr_note, True)
                elif len(series) < 2:
                    self._set_item(r, iid, "已查证（含模型判断）", "待核验", indep, 0, self.F.format(d),
                                   "待复核：各方模型属同一系列，且没有可信网站的独立来源支持" + yr_note, True)
                elif not diff_sites:
                    self._set_item(r, iid, "已查证（含模型判断）", "待核验", indep, 0, self.F.format(d),
                                   f"待复核：两方引用同一网站（{cited_txt}），且没有找到另一可信网站的独立来源" + yr_note, True)
                else:
                    self._set_item(r, iid, "已查证（含模型判断）", "待核验", indep, count, self.F.format(d),
                                   f"待复核：两方一致，但没有找到可信网站的独立来源（或独立来源没讲到{self.F.noun}）" + yr_note, True)
            elif len(dates) >= 2:
                settled = all(states.get(p) == "有争议" and json.loads(ok[p]["fields"]).get("异说") for p in dates)
                desc = "；".join(f"{p}：{self.F.value(json.loads(ok[p]['fields']))}（{json.loads(ok[p]['fields'])['出处']}）"
                                for p in dates)
                if last_round:
                    settled = True
                terminal = terminal and settled
                for p in dates:
                    if settled:
                        self._close_opinions(r, p, iid, ["分歧"], "各方已如实登记为有争议并列出异说")
                    else:
                        own = json.loads(ok[p]["fields"])
                        self._add_opinion(r, p, iid, "分歧",
                                          f"条目 {iid} 各方已查证的{self.F.key}不一致（{desc}），请复核来源；"
                                          f"确属来源分歧的，状态填有争议并在异说中列出另一说法",
                                          f"条目 {iid}：另一位生产者查到的{self.F.key}与你的 {self.F.value(own)}（{own.get('出处')}）"
                                          f"不一致。请只依据你自己检索到的原文复核；确属来源分歧的，状态填有争议并在异说中列出另一说法"
                                          f"（程序不告诉你对方的数值与出处，以免互相牵引）")
                gzs = {self._ganzhi(iid, [ok[p]], dates[p]) if not self.F.is_partial(dates[p]) else None for p in dates}
                z_ok = len(gzs) == 1 and None not in gzs
                self.p.exec("UPDATE items SET ganzhi=? WHERE item_id=?", (next(iter(gzs)) if z_ok else None, iid))
                self._set_item(r, iid, "已查证（含模型判断）", "待核验", None, 0, None,
                               ("有争议（来源分歧，已列异说）：" if settled else "有争议（待复核）：") + desc
                               + ("；" + self.T.same_category_note(next(iter(gzs))) if z_ok else ""), settled)
            elif states and all(s == "资料缺载" for s in states.values()) and len(states) == len(self.producers):
                self._set_item(r, iid, "未查证", "未验证", None, 0, "资料缺载", "各方均称资料缺载", True)
            else:
                others_missing = [p for p in self.producers if p not in dates]
                one = len(dates) == 1
                done = one and all(states.get(p) == "资料缺载" for p in others_missing)
                if one and (done or last_round):
                    p1 = next(iter(dates))
                    self._set_item(r, iid, "已查证（含模型判断）", "待核验", None, 0, self.F.format(dates[p1]),
                                   f"仅一方查到：只有 {p1} 的结果通过查证" +
                                   ("，其余各方称资料缺载" if done else "，其余各方到最后一轮仍未通过查证"), True)
                    continue
                if last_round:
                    self._set_item(r, iid, "未查证", "未验证", None, 0, None,
                                   f"到最后一轮仍未得出；已通过查证的生产者：{sorted(dates) or '无'}", True)
                    continue
                stalled = [p for p in others_missing if self._stalled(p, iid, r)]
                if others_missing and len(stalled) == len(others_missing):
                    # 1.6：没通过的各方连续两轮交了一模一样的内容、被拦的原因也一样，再退回也只会原样重交：停在这里
                    why = f"{'、'.join(stalled)} 连续两轮提交相同内容、被拦原因相同，程序停止重复退回（停在第 {r} 轮，需人工查看）"
                    if one:
                        p1 = next(iter(dates))
                        self._set_item(r, iid, "已查证（含模型判断）", "待核验", None, 0, self.F.format(dates[p1]),
                                       f"仅一方查到：只有 {p1} 的结果通过查证；{why}", True)
                    else:
                        self._set_item(r, iid, "未查证", "未验证", None, 0, None, f"未得出：{why}", True)
                    for p in stalled:
                        self._close_opinions(r, p, iid, ["未通过查证"], why)
                    continue
                terminal = False
                for p in others_missing:
                    if p in stalled:
                        continue
                    b = latest[p]
                    why = "、".join(json.loads(b["flags"] or "[]")) if b else "没有提交条目块"
                    msg = (f"条目 {iid}：你的条目块未通过查证（{b['st_check'] if b else '缺失'}：{why}），"
                           f"请重新检索并从原文逐字摘抄原句；确实查不到的如实填资料缺载")
                    self._add_opinion(r, p, iid, "未通过查证", msg, msg)
                self._set_item(r, iid, "未查证", "未验证", None, 0, None,
                               f"已通过查证的生产者：{sorted(dates) or '无'}"
                               + ("（仅一方查到，其余方继续查）" if one else ""), False)
        open_n = self.p.q("SELECT COUNT(*) c FROM opinions WHERE status IN ('提出','核验中')")[0]["c"]
        new_err = self.p.q("SELECT COUNT(*) c FROM reviews WHERE round=? AND verdict='错误'", (r,))[0]["c"]
        nov = self._novelty
        changed = sum(v[0] for v in nov.values())
        ratio = (sum(v[1] for v in nov.values()) / len(nov)) if nov else 0
        low = int(r > 1 and ratio < 0.10 and changed < 1)
        self.p.exec("INSERT OR REPLACE INTO round_stats VALUES (?,?,?,?,?,?,?)",
                    (r, new_err, open_n, changed, round(ratio, 4), low, None))
        self.p.log("程序", "状态更新", round_=r,
                   content={"未闭合意见": open_n, "新发现错误": new_err, "改动块": changed, "低新颖": bool(low)})
        return terminal and open_n == 0

    def _stalled(self, p, iid, r):
        """1.6：该生产者本轮与上一轮在这个条目上交的内容一模一样，程序拦下的原因也一样。"""
        cur = self.p.q("SELECT fields, flags FROM blocks WHERE author=? AND item_id=? AND round=? AND st_valid='有效'",
                       (p, iid, r))
        prev = self.p.q("SELECT fields, flags FROM blocks WHERE author=? AND item_id=? AND round=? AND st_valid='有效'",
                        (p, iid, r - 1))
        if not cur or not prev:
            return False
        core = lambda fl: sorted(x for x in json.loads(fl or "[]") if not x.startswith("说明文字中出现"))
        # 只比决定能否通过查证的字段（异说、备注之类每轮追加文字不算有实质改动）
        key = lambda fs: {k: json.loads(fs).get(k) for k in ("状态", "出处", "原句", *self.F.stall_keys())}
        return key(cur[0]["fields"]) == key(prev[0]["fields"]) and core(cur[0]["flags"]) == core(prev[0]["flags"])

    def _set_item(self, r, iid, st_check, st_verify, st_indep, count, value, note, final=False):
        row = self.p.q("SELECT history FROM items WHERE item_id=?", (iid,))[0]
        hist = json.loads(row["history"])
        hist.append({"round": r, "查证": st_check, "验证": st_verify, "独立性": st_indep,
                     "独立验证次数": count, "值": value, "说明": note, "终态": bool(final)})
        self.p.exec("UPDATE items SET st_check=?,st_verify=?,st_indep=?,indep_count=?,value=?,note=?,history=? "
                    "WHERE item_id=?", (st_check, st_verify, st_indep, count, value, note,
                                        json.dumps(hist, ensure_ascii=False), iid))
        if final:  # 条目已定论：其上未闭合的意见一并闭合
            for o in self.p.q("SELECT opinion_id FROM opinions WHERE item_id=? AND status IN ('提出','核验中')", (iid,)):
                self.p.exec("UPDATE opinions SET status='已解决', closed_round=? WHERE opinion_id=?",
                            (r, o["opinion_id"]))
                self.p.log("程序", "意见闭合", round_=r, ref_type="opinion", ref_id=o["opinion_id"],
                           content={"依据": f"条目已定论：{note}"})
