"""数学最终答案题的评审流程（2.2）。

只有【生产者】做题；【审查者】不做题，只做两件事：按表示层、方法层、约束层、验证层比对各生产者的提交，
并按生产者的文字描述独立写一份验证代码运行（这是审查者唯一"动手算"的地方）。
一轮 ＝ 各生产者做一次（第 1 轮是作答，之后是修改）→ 程序运行代码 → 审查者完整地审查一遍（每轮都是全新窗口，
没有上一轮的记忆）→ 意见发回各生产者（互相隔离，不含他人答案）。
闭合：本轮审查者没有任何硬伤意见、没有对象被评为有问题/不确定、没有验证不一致，并且各答案真的一致
（程序比对自动一致，或程序拿不准时全体审查者判为等价并标注"含模型判断"）。一致即提前结束。
未闭合：连续两轮没有实质改动（停滞）或达到轮数上限 → 标"有争议"，列出各方理由，交给用户裁决。
全部结束后程序才拿标准答案做比对，标准答案从不进入任何提示词。
沿用同一套账本、并行、断点续跑：整个流程是"调用存档 + 代码运行存档"的确定性回放，重新运行不会重复调用已成功的部分。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from . import runner
from .fields.answer import compare, compare_detail, normalize
from .models import CallFailedPause
from .tasks import final_answer as T
from .util import now_iso


class _Item:
    def __init__(self, it):
        self.it = it
        self.id = it["id"]
        self.subs = {}            # 生产者 -> [每轮的提交]
        self.ops_to = {}          # 生产者 -> 上一轮发给它的意见
        self.ops_hist = {}        # 生产者 -> {轮次: 那一轮结束后发给它的意见}
        self.ver_hist = {}        # 生产者 -> {轮次: [审查者对它那份提交的独立验证记录]}
        self.reviews = {}         # 轮次 -> [审查结果]
        self.stall = 0
        self.done = False
        self.final = None
        self.undeclared = set()


class ReviewFlow:
    def __init__(self, project, settings, hub, retriever=None):
        self.p, self.s, self.hub = project, settings, hub
        self.cfg = project.config
        self.items = self.cfg["items"]
        roles = settings["roles"]
        self.producers = [r for r, v in roles.items() if v.get("kind") == "生产者"]
        self.reviewers = [r for r, v in roles.items() if v.get("kind") == "审查者"]
        if not self.producers or not self.reviewers:
            raise ValueError("最终答案题需要至少 1 个生产者和 1 个审查者（在 设置.json 里给每个角色写 kind）；"
                             "审查者不做题，只审查。")
        params = {**settings["params"], **(self.cfg.get("params") or {})}
        self.cap = int(params.get("review_rounds", 5))
        self.max_tokens = int(params.get("answer_max_tokens", 65536))
        self.lang = params.get("output_language") or "中文"
        self.allow_code = bool(params.get("allow_code_run", False))
        self.code_timeout = int(params.get("code_timeout_seconds", 60))
        self.window = {r: self._window_of(roles[r]) for r in self.producers}
        self.label = {r: T.LABELS[i] for i, r in enumerate(self.producers)}
        self.by_label = {v: k for k, v in self.label.items()}
        self.hub.defer_pause = True  # 2.5：某角色失败时，等同一步里其他角色都跑完再记"已暂停"
        self._once = threading.Lock()
        self._runlock = threading.Lock()
        self.libs = {}
        self.op_text = {}
        self._init_items()

    def _scope(self, it):
        """口径：题目条目里自带的优先，否则用项目的口径。"""
        return it.get("scope") or self.cfg.get("scope") or ""

    @staticmethod
    def _window_of(spec):
        """生产者在修改轮里能看到自己最近几轮的完整记录（None＝全部）。读设置里的记忆模式，数学题的含义：
        全局＝保留自己的全部轮次（永远看不到别的生产者，隔离不变）；固定＝最近 rounds 轮；全新＝只带上一轮。
        没有设置时默认保留全部。"""
        m = spec.get("memory")
        if not isinstance(m, dict):
            return None
        mode = m.get("mode")
        if mode == "全新":
            return 1
        if mode == "固定":
            try:
                return max(1, int(m.get("rounds", 1)))
            except (TypeError, ValueError):
                return 1
        return None

    def _history(self, st, role, r):
        lo = 1 if self.window[role] is None else max(1, r - self.window[role])
        out = []
        for k in range(lo, r):
            out.append((k, {"sub": st.subs[role][k - 1], "ops": st.ops_hist.get(role, {}).get(k, []),
                            "ver": st.ver_hist.get(role, {}).get(k, [])}))
        return out

    # ---------------- 基础 ----------------
    def _init_items(self):
        for it in self.items:
            if not self.p.q("SELECT 1 FROM items WHERE item_id=?", (it["id"],)):
                self.p.exec("INSERT INTO items (item_id,name,type,st_check,st_verify,st_indep,st_valid,history)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (it["id"], it.get("name") or it["problem"][:30], "题", "未作答", "未验证", None, "有效", "[]"))

    def is_finished(self):
        return bool(self.p.q("SELECT 1 FROM events WHERE type='迭代结束'"))

    def _log_once(self, actor, etype, ref_id, content, round_=None):
        with self._once:
            if self.p.q("SELECT 1 FROM events WHERE type=? AND ref_id=?", (etype, ref_id)):
                return
            self.p.log(actor, etype, round_=round_, ref_type="key", ref_id=ref_id, content=content)

    def _pool(self, jobs):
        first = None
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
            for fut in [ex.submit(j) for j in jobs]:
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001
                    first = first or e
        if first:
            raise first

    # ---------------- 代码运行（存档，重跑不重复运行） ----------------
    def _run(self, role, iid, r, kind, code, tag=""):
        if not self.allow_code or not (code or "").strip():
            return None
        purpose = f"{iid}|{role}|{r}|{tag}"
        with self._runlock:
            old = self.p.q("SELECT run_id, output, status FROM runs WHERE purpose_items=? AND run_type=?",
                           (purpose, kind))
            if old:
                d = json.loads(old[0]["output"])
                d["run_id"] = old[0]["run_id"]
                return d
        res = runner.run_code(code, timeout=self.code_timeout)
        with self._runlock:
            rid = self.p.next_id("runs", "R", "run_id")
            self.p.exec("INSERT INTO runs (run_id,initiator,round,run_type,purpose_items,code,code_hash,output,status,ts)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, role, r, kind, purpose, code, res["code_hash"], json.dumps(res, ensure_ascii=False),
                         res["status"], now_iso()))
        res["run_id"] = rid
        self._log_once("程序", "代码运行", rid, {"initiator": role, "item": iid, "round": r, "类型": kind,
                                             "状态": res["status"], "用时": res["seconds"], "code_hash": res["code_hash"],
                                             "环境": res.get("env")}, r)
        return res

    def _setup_env(self):
        self.libs = runner.probe_libs() if self.allow_code else {}
        cur = {"允许运行代码": self.allow_code, "超时秒数": self.code_timeout,
               "隔离级别": "A档：子进程+超时+输出上限+临时目录+不传密钥；不是安全隔离",
               "可用库": self.libs}
        # 2.6 修：用户停止后改了设置再继续，新设置也要记进账本（2.5 只记第一次，报告因此误写"没有运行任何代码"）
        with self._once:
            rows = self.p.q("SELECT content FROM events WHERE type='代码运行设置' ORDER BY seq")
            last = json.loads(rows[-1]["content"]) if rows else None
            if last is None or last.get("允许运行代码") != cur["允许运行代码"] or last.get("超时秒数") != cur["超时秒数"]:
                ref = "设置" if not rows else f"设置#{len(rows) + 1}"
                if rows:
                    cur["说明"] = "运行中途改过设置（用户停止后修改再继续）"
                self.p.log("程序", "代码运行设置", ref_type="key", ref_id=ref, content=cur)

    # ---------------- 生产者 ----------------
    def _producer(self, role, st, r, force_carry=False, fresh_key=None):
        """2.9.1：force_carry＝沿用已有的答案（用户补充后"先进审查者"的那一轮）；fresh_key＝这位生产者手里还没有任何答案，
        用第 1 轮的方式作答（调用编号另起，不与被直接打断作废的调用重名）。"""
        iid, it = st.id, st.it
        prev = st.subs[role][-1] if st.subs[role] else None
        ops = st.ops_to.get(role, []) if r > 1 and not fresh_key else []
        if prev is not None and (force_carry or (r > 1 and not ops and not fresh_key)):  # 没有针对它的意见：沿用上一轮，不浪费一次调用，也不泄露别人的信息
            sub = dict(prev, round=r, carried=True, responses=[], changes=[], warnings=[],
                       author_check={"failed": False, "status": "沿用上一轮", "tail": ""})
            st.subs[role].append(sub)
            self._log_once(role, "提交", f"{iid}|{role}|{r}", dict(sub, item=iid, role=role), r)
            return
        if r == 1 or fresh_key or prev is None:
            obj = self.hub.call_json(role, T.SYSTEM_PRODUCER, T.solve_prompt(it, self.libs, self.allow_code, self._scope(it), self.lang),
                                     call_key=fresh_key or f"p-{role}-{iid}-r{r}", round_=r, validator=T.validate_submission(None),
                                     max_tokens=self.max_tokens)
        else:
            obj = self.hub.call_json(role, T.SYSTEM_REVISER,
                                     T.revise_prompt(it, self._history(st, role, r), ops, self.libs, self.allow_code,
                                                    self._scope(it), self.lang),
                                     call_key=f"p-{role}-{iid}-r{r}", round_=r,
                                     validator=T.validate_submission([o["id"] for o in ops]), max_tokens=self.max_tokens)
        run = self._run(role, iid, r, "作者验证", obj["code"], "作者")
        sub = {"round": r, "carried": False, "status": obj["status"], "answer": obj["answer"],
               "author_check": T.author_check(obj["status"], run),
               "segments": obj.get("segments", []), "unknown": obj.get("unknown", {}), "reasoning": obj["reasoning"],
               "sympy": obj.get("sympy", ""), "verify_plan": obj["verify_plan"], "code": obj["code"],
               "limits": obj["limits"], "responses": obj["responses"], "changes": obj["changes"], "run": run,
               "warnings": obj.get("warnings", [])}
        if prev and (normalize(sub["answer"]) != normalize(prev["answer"]) or sub["status"] != T.status_of(prev)) \
                and not sub["changes"]:
            st.undeclared.add(role)
        st.subs[role].append(sub)
        self._log_once(role, "提交", f"{iid}|{role}|{r}", dict(sub, item=iid, role=role), r)
        for resp in sub["responses"]:
            self.p.exec("UPDATE opinions SET status=?, evidence=? WHERE opinion_id=?",
                        (resp["decision"], resp["check"], resp["opinion"]))
            self._log_once(role, "意见回应", f"{resp['opinion']}|{r}", dict(resp, round=r, item=iid), r)

    # ---------------- 审查者 ----------------
    def _facts(self, answers):
        labs = list(answers)
        if len(labs) < 2:
            return "只有一位生产者"
        out = [f"{l}：{T.status_of(answers[l])}" for l in labs if T.status_of(answers[l]) != "得出"]
        known = [l for l in labs if T.status_of(answers[l]) == "得出"]
        for i in range(len(known)):
            for j in range(i + 1, len(known)):
                x, y = answers[known[i]], answers[known[j]]
                d = compare_detail(x["answer"], y["answer"], x.get("sympy"), y.get("sympy"))
                out.append(f"{known[i]}与{known[j]}：{d['result']}（{d['how']}）")
        return "；".join(out)

    def _views(self, st, r):
        views = []
        for role in self.producers:
            s = st.subs[role][-1]
            resp = [dict(x, opinion_text=self.op_text.get(x["opinion"], x["opinion"])) for x in s["responses"]]
            views.append({"label": self.label[role], "status": T.status_of(s), "answer": s["answer"],
                          "segments": s.get("segments", []), "unknown": s.get("unknown", {}), "reasoning": s["reasoning"],
                          "verify_plan": s["verify_plan"], "limits": s["limits"], "responses": resp,
                          "changes": s["changes"], "author_ran": s["run"] is not None if self.allow_code else None})
        return views

    def _review_call(self, rv, st, r, labels, facts):
        """审查的第一步（2.7 起子类可替换成"分开审、再合起来对照"）。返回含 overall、opinions、implementations、
        unverified_parts、same_route、answers_equivalent、fallback_answer 的字典。"""
        it = st.it
        return self.hub.call_json(rv, T.SYSTEM_REVIEWER, T.review_prompt(it, self._views(st, r), facts, self.libs, self.allow_code,
                                                       self._scope(it), self.lang),
                                  call_key=f"rv1-{rv}-{st.id}-r{r}", round_=r, validator=T.validate_review(labels),
                                  max_tokens=self.max_tokens)

    def _reviewer(self, rv, st, r):
        iid, it = st.id, st.it
        labels = [self.label[p] for p in self.producers]
        answers = {self.label[p]: st.subs[p][-1] for p in self.producers}
        facts = self._facts(answers)
        a = self._review_call(rv, st, r, labels, facts)
        ops = list(a["opinions"])
        # 独立验证：审查者按生产者的文字描述自己写的实现，由程序运行
        impls = {}
        for im in a["implementations"]:
            if im["target"] not in impls:
                impls[im["target"]] = im
        rows, ver, unverified = [], [], []
        for lab in labels:
            role = self.by_label[lab]
            sub = st.subs[role][-1]
            if T.status_of(sub) == "未得出":  # 没有答案，也就没有可验证的东西；它的理由成不成立由评审意见判断
                ver.append({"target": lab, "result": "不适用", "diagnosis": "无", "reason": "生产者未得出，没有可验证的答案"})
                continue
            im = impls.get(lab)
            irun = self._run(rv, iid, r, "独立实现", im["code"], f"实现{lab}") if im else None
            if sub["run"] is not None or irun is not None:
                rows.append({"label": lab, "verify_plan": sub["verify_plan"], "author_run": sub["run"], "impl_run": irun,
                             "impl_note": (im["explain"] or "审查者照描述写的实现") if im else "审查者没有写实现",
                             "check_type": im["check_type"] if im and irun is not None else "",
                             "check_note": im["check_note"] if im and irun is not None else ""})
            else:
                why = ("未允许运行代码" if not self.allow_code else
                       ("生产者没有写验证代码" if not sub["code"].strip() else "没有可运行的结果") +
                       ("，审查者也没有写实现" if not im else ""))
                ver.append({"target": lab, "result": "未能验证", "diagnosis": "无", "reason": why})
                unverified.append(f"{lab}：{why}")
        if rows:
            b = self.hub.call_json(rv, T.SYSTEM_VERIFIER, T.verify_prompt(it, rows, labels, self._scope(it), self.lang),
                                   call_key=f"rv2-{rv}-{iid}-r{r}", round_=r,
                                   validator=T.validate_verify(labels, [x["label"] for x in rows]),
                                   max_tokens=self.max_tokens)
            ver += b["verifications"]
            ops += b["opinions"]
            unverified += b["unverified"]
            for row in rows:
                # 2.5：程序自己补一条（不靠模型自觉）：只做了同路线复现，就是"只核对了实现，没核对模型假设"
                if row["impl_run"] is not None and row["check_type"] in ("同路线复现", "未能换方法"):
                    unverified.append(f"{row['label']}：仅核对实现，未核对模型假设（核对类型：{row['check_type']}"
                                      + (f"，{row['check_note']}" if row["check_note"] else "") + "）")
                if row["author_run"] and row["author_run"]["status"] != "完成":
                    unverified.append(f"{row['label']}的验证代码运行{row['author_run']['status']}")
        for v in ver:  # 核对类型写进每条独立验证记录（没写实现、没运行的显示"无"）
            row = next((x for x in rows if x["label"] == v["target"]), None)
            v["check_type"] = (row["check_type"] if row and row["check_type"] else "无")
            v["check_note"] = row["check_note"] if row else ""
        res = {"reviewer": rv, "overall": a["overall"], "same_route": a["same_route"],
               "unver_parts": a["unverified_parts"],
               "answers_equivalent": a["answers_equivalent"], "ops": ops, "ver": ver, "unverified": unverified,
               "fallback": a["fallback_answer"],
               "runs": {row["label"]: {"作者": row["author_run"]["status"] if row["author_run"] else "未运行",
                                       "独立": row["impl_run"]["status"] if row["impl_run"] else "未写/未运行"} for row in rows}}
        with self._once:
            for v in ver:
                role = self.by_label[v["target"]]
                row = next((x for x in rows if x["label"] == v["target"]), None)
                im = impls.get(v["target"])
                st.ver_hist.setdefault(role, {}).setdefault(r, []).append({
                    "reviewer_label": rv, "result": v["result"], "diagnosis": v["diagnosis"], "reason": v["reason"],
                    "check_type": v.get("check_type"), "check_note": v.get("check_note"),
                    "impl_code": im["code"] if (im and row and row["impl_run"] is not None) else "",
                    "impl_note": im["explain"] if im else "", "impl_run": row["impl_run"] if row else None})
        self._record_review(st, r, res)
        return res

    def _record_review(self, st, r, res):
        iid, rv = st.id, res["reviewer"]
        self._log_once(rv, "评审", f"{iid}|{rv}|{r}", {
            "item": iid, "round": r, "总体评价": res["overall"], "路线相同": res["same_route"],
            "答案等价": res["answers_equivalent"], "意见数": len(res["ops"]), "独立验证": res["ver"],
            "未实测或不确定项": res["unverified"], "未验证的部分": res["unver_parts"], "审查者独立求得的答案": res["fallback"] or None, "运行": res["runs"]}, r)
        res["op_ids"] = []
        for n, o in enumerate(res["ops"], 1):
            oid = f"{iid}.r{r}.{rv}.{n}"
            role = self.by_label[o["target"]]
            others = [st.subs[x][-1]["answer"] for x in self.producers if x != role]
            text, k = T.mask_foreign(o["problem"], others, [self.label[x] for x in self.producers if x != role])
            o.update({"id": oid, "role": role, "sent": text, "masked": k})
            self.op_text[oid] = text
            res["op_ids"].append(oid)
            if not self.p.q("SELECT 1 FROM opinions WHERE opinion_id=?", (oid,)):
                self.p.exec("INSERT INTO opinions (opinion_id,source_role,review_id,target_author,item_id,content,status,"
                            "closed_round,evidence,kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (oid, rv, f"{iid}|{rv}|{r}", role, iid, json.dumps(o, ensure_ascii=False), "提出", None, None,
                             o["layer"]))
            self._log_once(rv, "评审意见", oid, {"item": iid, "round": r, "对象": role, "层": o["layer"],
                                              "严重程度": o["severity"], "被反驳的依据": o.get("basis") or None, "位置": o["location"], "发出的意见": text,
                                              "原文": o["problem"], "对比说明（不发给生产者）": o["internal_note"],
                                              "抹去他人信息处数": k}, r)

    # ---------------- 判定 ----------------
    def _known(self, st):
        return [p for p in self.producers if T.status_of(st.subs[p][-1]) == "得出"]

    def _answers_status(self, st, known=None):
        """各个"得出"的答案之间是否一致；未得出、部分得出的不参与比较（弃权不是反对）。"""
        known = self._known(st) if known is None else known
        ans = [st.subs[p][-1] for p in known]
        cmp = lambda x, y: compare(x["answer"], y["answer"], x.get("sympy"), y.get("sympy"))  # noqa: E731
        res = []
        for i in range(len(ans)):
            for j in range(i + 1, len(ans)):
                res.append(cmp(ans[i], ans[j]))
        if "不一致" in res:
            return "不一致"
        return "待人工判断" if "待人工判断" in res else "自动一致"

    def _evaluate(self, st, r):
        """2.7 拆出来：只判断、不改状态（找茬者要先知道"这一轮结束后流程会不会结束"）。"""
        revs = st.reviews[r]
        n = len(self.producers)
        hard = [o for rv in revs for o in rv["ops"] if o["severity"] == "硬伤"]
        # 2.5：只有"有问题"（＝被反驳，且必有硬伤意见）才算数；"不确定"不挡闭合，由 _final 记进"未验证的部分"
        bad_overall = [(rv["reviewer"], l, v) for rv in revs for l, v in rv["overall"].items() if v == "有问题"]
        ver_bad = [(rv["reviewer"], x["target"]) for rv in revs for x in rv["ver"] if x["result"] == "不一致"]
        known = self._known(st)
        part = [p for p in self.producers if T.status_of(st.subs[p][-1]) == "部分得出"]
        status = self._answers_status(st, known) if known else "无完整答案"
        how = None
        agree = False
        enough = True
        outcome = "得出"
        if known:
            if status == "自动一致":
                agree, how = True, "程序比对自动一致"
            elif status == "待人工判断" and len(known) >= 2 and all(rv["answers_equivalent"] == "是" for rv in revs):
                agree, how = True, "含模型判断：程序拿不准，审查者判为等价"
            enough = len(known) >= min(2, n)  # 多个生产者时，至少两位得出且一致才算闭合；只有一位得出只能标"仅一方得出"
            if agree and len(known) < n:
                how += f"（另有 {n - len(known)} 位生产者未完整得出，不算反对，已在报告里注明）"
        else:
            agree, how = True, "各生产者都没有得出完整答案"
            outcome = "部分得出" if part else "未得出"
        blocking = bool(hard or bad_overall or ver_bad)
        closed = agree and enough and not blocking
        # 停滞检测
        changed = False
        got = False
        stall = st.stall
        if r > 1:
            for role in self.producers:
                cur, prv = st.subs[role][-1], st.subs[role][-2]
                if not cur["carried"]:
                    got = True
                if (normalize(cur["answer"]) != normalize(prv["answer"]) or cur["code"] != prv["code"]
                        or cur["changes"] or T.status_of(cur) != T.status_of(prv)):
                    changed = True
            stall = st.stall + 1 if (got and not changed) else 0
        # 没有任何意见可发时，再迭代也不会有变化
        nothing_to_send = not any(rv["ops"] for rv in revs)
        reason = None
        if not closed:
            if stall >= 2:
                reason = "停滞：连续两轮各方没有实质改动，意见没有被接受也没有被说服"
            elif nothing_to_send:
                reason = "没有可发给生产者的意见，继续迭代不会有变化"
            elif r >= getattr(st, "cap", self.cap):  # 2.9.1：用户补充后，从"先进审查者"那一轮起重新给满轮数
                reason = f"达到轮数上限（{self.cap} 轮）仍未闭合" + ("（从用户补充后重新计）" if getattr(st, "cap", self.cap) != self.cap else "")
        summary = {"item": st.id, "round": r, "答案比对": status, "硬伤意见数": len(hard),
                   "各方状态": {self.label[p]: T.status_of(st.subs[p][-1]) for p in self.producers},
                   "未通过的对象": [f"{a}:{l}={v}" for a, l, v in bad_overall], "验证不一致": [f"{a}:{t}" for a, t in ver_bad],
                   "闭合": closed, "停滞计数": stall}
        return {"closed": closed, "reason": reason, "how": how, "outcome": outcome, "known": known,
                "blocking": blocking, "stall": stall, "summary": summary}

    def _decide(self, st, r):
        revs = st.reviews[r]
        ev = self._evaluate(st, r)
        st.stall = ev["stall"]
        closed, reason = ev["closed"], ev["reason"]
        self._log_once("程序", "轮次小结", f"{st.id}|{r}", ev["summary"], r)
        if closed or reason:
            st.done = True
            st.final = self._final(st, r, closed, ev["how"], reason, ev["outcome"], ev["known"], ev["blocking"])
            return
        st.ops_to = {p: [] for p in self.producers}
        for rv in revs:
            for o in rv["ops"]:
                st.ops_to[o["role"]].append({"id": o["id"], "layer": o["layer"], "severity": o["severity"], "basis": o.get("basis", ""),
                                             "location": o["location"], "problem": o["sent"]})
        for p in self.producers:
            st.ops_hist.setdefault(p, {})[r] = list(st.ops_to[p])

    def _final(self, st, r, closed, how, reason, outcome="得出", known=None, blocking=False):
        revs = st.reviews[r]
        known = self._known(st) if known is None else known
        first = {p: T.answer_text(st.subs[p][0]) for p in self.producers}
        last = {p: T.answer_text(st.subs[p][-1]) for p in self.producers}
        ind = []
        series = [self.s["roles"][p].get("series") for p in self.producers]
        if closed and outcome == "得出" and len(known) > 1:
            if len(set(series)) < len(series):
                ind.append("有生产者属于同一系列的模型，一致但独立性未确认")
            if any(rv["same_route"] for rv in revs):
                ind.append("审查者判定各生产者走的是本质相同的路线，一致但独立性未确认")
        ver = []
        for p in self.producers:
            lab = self.label[p]
            rr = [rv["runs"].get(lab) for rv in revs if rv["runs"].get(lab)]
            vs = [x for rv in revs for x in rv["ver"] if x["target"] == lab]
            cts = sorted({(x.get("check_type") or "无") for x in vs if (x.get("check_type") or "无") != "无"})
            notes = [x["check_note"] for x in vs if x.get("check_note")]
            ver.append({"生产者": p, "作者代码": "、".join(sorted({x["作者"] for x in rr})) or "未运行",
                        "独立实现": "、".join(sorted({x["独立"] for x in rr})) or "未写/未运行",
                        "结论": "、".join(sorted({x["result"] for x in vs})) or "未验证",
                        "诊断": "、".join(sorted({x["diagnosis"] for x in vs if x["diagnosis"] != "无"})),
                        "核对类型": "、".join(cts) or "无",
                        "核对说明": "；".join(dict.fromkeys(notes))})
        unv = []
        for rv in revs:
            for u in rv["unverified"]:
                if u not in unv:
                    unv.append(u)
        # 2.5 未验证的部分：不挡闭合，但必须随结论一起引用。来源四种：审查者列出（含"试过什么"）、
        # 审查者验证步骤的未实测项、程序补的（仅同路线复现等）、生产者自己写的限制
        parts = []
        for rv in revs:
            for u in rv.get("unver_parts", []):
                parts.append({"来源": f"审查者{rv['reviewer']}", "对象": u["target"], "内容": u["what"], "试过": u["tried"]})
        for rv in revs:
            for l, v in rv["overall"].items():
                if v == "不确定":
                    parts.append({"来源": f"程序（审查者{rv['reviewer']}评为不确定，不挡闭合）", "对象": l,
                                  "内容": "审查者无法判断这位生产者的提交", "试过": ""})
        for u in unv:
            parts.append({"来源": "审查者验证步骤/程序", "对象": "", "内容": u, "试过": ""})
        for p in self.producers:
            real = [sb for sb in st.subs[p] if not sb.get("carried")]
            lim = (real[-1].get("limits") or "").strip() if real else ""
            if lim and lim not in ("无", "（无）"):
                parts.append({"来源": f"生产者{self.label[p]}自述的限制", "对象": self.label[p], "内容": lim, "试过": ""})
        author_fail = []
        for p in self.producers:
            real = [sb for sb in st.subs[p] if not sb.get("carried")]
            if real and real[-1].get("author_check", {}).get("failed"):
                ac = real[-1]["author_check"]
                author_fail.append({"生产者": p, "标签": self.label[p], "轮": real[-1]["round"], "状态": ac["status"],
                                    "摘要": ac["tail"]})
        soft = [f"{self.label[o['role']]}：{o['sent']}" for rv in revs for o in rv["ops"] if o["severity"] == "次要"]
        n = len(self.producers)
        if closed:
            status = "闭合" if outcome == "得出" else outcome  # 闭合 / 未得出 / 部分得出
        elif len(known) == 1 and n >= 2 and not blocking:
            status = "仅一方得出"
        else:
            status = "有争议"
        consensus = None
        if status in ("闭合", "仅一方得出"):
            consensus = st.subs[known[0]][-1]["answer"]
        partial = {self.label[p]: {"状态": T.status_of(st.subs[p][-1]), "已确定": T.segments_text(st.subs[p][-1]) or st.subs[p][-1]["answer"],
                                   "未解决": T.unknown_text(st.subs[p][-1])}
                   for p in self.producers if T.status_of(st.subs[p][-1]) != "得出"}
        warns = [f"{self.label[p]}（第 {k + 1} 轮）：{w}" for p in self.producers
                 for k, sb in enumerate(st.subs[p]) if not sb.get("carried") for w in sb.get("warnings", [])]
        content = {"状态": status, "原因": reason, "一致方式": how if closed else None,
                   "共识": consensus, "首轮": first, "最终": last, "轮数": r,
                   "各方最终状态": {p: T.status_of(st.subs[p][-1]) for p in self.producers},
                   "未得出或部分得出者": partial, "格式警告": warns,
                   "独立性提示": ind, "未声明的改答案": sorted(st.undeclared), "未处理次要意见": soft,
                   "验证概况": ver, "未实测项": unv, "未验证的部分": parts, "作者验证未通过却提交": author_fail}
        if status in ("有争议", "仅一方得出"):
            content["争议详情"] = [{"生产者": p, "答案": last[p],
                               "收到的意见": [f"{o['layer']}/{o['severity']}：{o['sent']}" for rv in revs for o in rv["ops"]
                                          if o["role"] == p],
                               "回应": [f"{x['decision']}：{x['check']}" for x in st.subs[p][-1]["responses"]]}
                              for p in self.producers]
        if closed:  # 已回应过的意见在审查者通过后算已解决
            self.p.exec("UPDATE opinions SET status='已解决', closed_round=? WHERE item_id=? AND status IN ('采纳','驳回')",
                        (r, st.id))
        return content

    # ---------------- 主流程 ----------------
    def run(self):
        if not self.p.q("SELECT 1 FROM events WHERE type='准入判断'"):
            self.p.log("用户", "准入判断", content={"结果": "强制放行", "说明": "由用户在建题窗口或手动放行"})
        if self.is_finished():
            return "已结束"
        try:
            self._setup_env()
            states = [_Item(it) for it in self.items]
            for st in states:
                st.subs = {p: [] for p in self.producers}
            for r in range(1, self.cap + 1):
                active = [s for s in states if not s.done]
                if not active:
                    break
                self._pool([(lambda s=s, p=p: self._producer(p, s, r)) for s in active for p in self.producers])
                jobs = []
                for s in active:
                    s.reviews[r] = [None] * len(self.reviewers)

                    jobs += [(lambda s=s, i=i, rv=rv: s.reviews[r].__setitem__(i, self._reviewer(rv, s, r)))
                             for i, rv in enumerate(self.reviewers)]
                self._pool(jobs)
                for s in active:
                    self._decide(s, r)
            for s in states:
                if not s.done:  # 理论上不会走到（最后一轮必有结论），防御性处理
                    s.final = self._final(s, self.cap, False, None, "未知原因未闭合")
                    s.done = True
            for s in states:
                f = s.final
                self._log_once("程序", "最终状态", s.id, f)
                self.p.exec("UPDATE items SET st_check=?, value=? WHERE item_id=?", (f["状态"], f["共识"], s.id))
            self._compare_gold(states)
        except CallFailedPause as e:
            fl = list(self.hub.failures)
            self.p.log("程序", "调用失败暂停", ref_type="call_key", ref_id=e.call_key,
                       content={"role": e.role, "reason": e.reason,
                                "失败角色": [{"role": f["role"], "reason": f["reason"]} for f in fl] or
                                         [{"role": e.role, "reason": e.reason}]})
            return f"调用失败暂停：{e}。检查问题后重新运行即可从失败处继续。"
        self.p.log("程序", "迭代结束", content={"原因": "全部题目已闭合或已标为有争议"})
        return "完成"

    @staticmethod
    def _candidate(sub):
        """2.5：部分得出的"候选值"——answer 里写的已确定部分；没有 answer 时，只有一段"确定"的分段才取它。"""
        if (sub.get("answer") or "").strip():
            return sub["answer"].strip()
        sure = [x["value"] for x in (sub.get("segments") or []) if x.get("state") == "确定"]
        return sure[0] if len(sure) == 1 else ""

    def _cand_vs_gold(self, subs, gold):
        """候选值与标准答案是否一致：返回 (候选值文本, "自动一致"/"不一致"/"待人工判断"/None)。
        有多个候选值时：全部与标准答案自动一致才算一致，有一个不一致就是不一致，其余待人工判断。不靠模型。"""
        cands = [(self._candidate(x), x) for x in subs if self._candidate(x)]
        if not cands:
            return "", None
        res = [compare_detail(c, gold, x.get("sympy"), None, b_is_gold=True)["result"] for c, x in cands]
        txt = "；".join(c for c, _ in cands)
        if all(x == "自动一致" for x in res):
            return txt, "自动一致"
        if any(x == "不一致" for x in res):
            return txt, "不一致"
        return txt, "待人工判断"

    def _compare_gold(self, states):
        """全部结束后才碰标准答案。基线＝各生产者首轮；最终＝闭合（或仅一方得出）后的答案。
        2.4：未得出、部分得出不是错，单独记成"未得出""部分得出"，不拿去和标准答案比对。"""
        for s in states:
            iid, gold = s.id, s.it.get("gold")
            if gold in (None, ""):
                continue
            for role in self.producers:
                sub = s.subs[role][0]
                stt = T.status_of(sub)
                if stt != "得出":
                    cand, cres = self._cand_vs_gold([sub], gold) if stt == "部分得出" else ("", None)
                    self._log_once("程序", "对照标准答案", f"{iid}|基线:{role}",
                                   {"answer": T.answer_text(sub), "gold": gold, "result": stt, "判定方式": "生产者自己声明",
                                    "候选值": cand, "候选值与标准答案": cres})
                    continue
                a = sub["answer"]
                d = compare_detail(a, gold, sub.get("sympy"), None, b_is_gold=True)
                self._log_once("程序", "对照标准答案", f"{iid}|基线:{role}",
                               {"answer": a, "gold": gold, "result": d["result"], "判定方式": d["how"],
                                "错误层": d.get("layer")})
            fs = s.final["状态"]
            if fs in ("闭合", "仅一方得出"):
                kn = self._known(s)[0]
                a = s.final["共识"]
                d = compare_detail(a, gold, s.subs[kn][-1].get("sympy"), None, b_is_gold=True)
                self._log_once("程序", "对照标准答案", f"{iid}|最终",
                               {"answer": a, "gold": gold, "result": d["result"],
                                "判定方式": d["how"] + ("（仅一方得出）" if fs == "仅一方得出" else ""),
                                "错误层": d.get("layer")})
            elif fs in ("未得出", "部分得出"):
                part = [s.subs[p][-1] for p in self.producers if T.status_of(s.subs[p][-1]) == "部分得出"]
                cand, cres = self._cand_vs_gold(part, gold)
                self._log_once("程序", "对照标准答案", f"{iid}|最终",
                               {"answer": f"（{fs}）", "gold": gold, "result": fs, "判定方式": "各方都没有得出完整答案",
                                "候选值": cand, "候选值与标准答案": cres})
