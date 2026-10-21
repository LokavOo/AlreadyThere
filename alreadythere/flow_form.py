"""2.9.1：插话与打断（见 interject.py；用户 2026-10-21 定）。
- 用户的话＝用户指令：程序一字不改地接在题目后面（附在之后所有生产者、审查者、找茬者调用的提示词最前面），模型照做。
- 收到指令（或"直接打断"）的地方是一个"转折点"。转折点之后**永远先进审查者**：已有的内容全部保留（包括已经闭合的题，
  也包括打断时还在等、后来交回来的答案），审查者拿"原题＋补充"把它们审一遍，之后照正常流程走（被挑出问题的生产者返工，
  没问题的沿用）。已闭合的题重审没有问题就维持闭合并记一笔；挑出硬伤就重新打开。转折点之后每道题重新给满轮数。
  - 转折点在"生产者作答前／审查前"：这一轮就是"先审"的一轮（生产者沿用已有答案）；在"找茬前／本轮小结前"：这一轮余下的步骤
    不做，下一轮是"先审"的一轮。
  - 某个生产者手里一份答案都没有（例如第 1 轮就被直接打断），没有可审的，它就在"先审"的那一轮直接作答。
- 普通打断：等正在跑的调用全部返回（一个都不浪费）再暂停。直接打断：窗口掐断进程，正在跑的调用作废、不补；
  被切断的那一步里，只有已经存档的结果算数。

2.8：表单阶段改为开关，默认关（用户 2026-10-21 定）。关的时候不拆表单，生产者、审查者、找茬者都直接看原题和口径，
生产者第一轮的结果就是基线；开的时候与 2.7 完全一样。理由（用户）：2.7 的表单阶段源于把"选题错误"（6月 Q1 是审查者没有锚点、
本该被准入门拦下的题）误当成"模型读题错误"；2.8 回退这个错误。表单的代码保留，以后是否恢复、怎么做待定。
2.7 创建的项目仍按表单流程跑；2.8 创建的项目第一次运行时按参数面板的"表单阶段"定下，之后不再改变（记入账本）。

2.7：数学题两段式流程（表单阶段 → 做题阶段），在 2.2 评审流程（flow_review.ReviewFlow）的基础上扩展。

第一段 表单阶段（每道题）：各生产者独立把题面拆成表单（只拆不改写，程序逐字核对）→ 审查者一次看全部表单比对
→（按出场规则）找茬者看全部表单找共用前提和冲突 → 审查者复查找茬者的问题 → 意见发回生产者。
各方真的一致（没有硬伤、归类没有差异、术语展开和隐含项一致）才闭合；到上限仍不一致标"有争议"。
结束后交人放行（默认）；参数面板开了自动放行且条件全满足时自动冻结。冻结前不进入做题。

第二段 做题阶段：生产者、审查者、找茬者都只依据冻结的表单，不再给原题（用户 2026-10-03 定：这样才能分清问题出在
表单还是做题）。审查者"分开审、再合起来对照"：每份答卷单独审一次（逐条审查、额外前提、反驳够用即止、写摘要），
再做一次轻量对照（只看自己写的摘要，查材料间冲突、判断路线是否相同）；之后照旧做独立验证的比较。
找茬者按出场规则出场，看完整答卷（找到即止），意见只给审查者，审查者复查后采纳的才作为审查者的意见发给生产者。

找茬者出场规则（用户 2026-10-03 定，取代规格 1.4 原来的"每 2 轮一次＋程序触发"）：满足任一即出场——
①这是第一轮；②上一轮没出场；③这一轮结束后流程就要结束（在审查者第一次检查后判断：结果是闭合或已到轮数上限等）。
找茬者不挡闭合；审查者复查时采纳了被反驳的硬伤，闭合就撤销、进入下一轮。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from . import formcheck as FC
from . import interject as I
from .flow_review import ReviewFlow, _Item
from .models import CallFailedPause
from .tasks import final_answer as T
from .tasks import stage27 as S

FORM_REVIEW_BASES = S.FORM_BASES
SOLVE_RECHECK_BASES = tuple(T.BASES[:5])


class _FormItem:
    def __init__(self, it):
        self.it = it
        self.id = it["id"]
        self.forms = {}      # 生产者 -> [每轮的表单记录]
        self.ops_to = {}     # 生产者 -> 上一轮发给它的意见
        self.reviews = {}    # 轮次 -> [审查结果]
        self.prog_ops = {}   # 轮次 -> [程序给的意见（归类差异）]
        self.diffs = {}      # 轮次 -> 归类差异
        self.ff = set()      # 找茬者出场过的轮次
        self.stall = 0
        self.done = False
        self.final = None


def _pool(jobs):
    first = None
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        for fut in [ex.submit(j) for j in jobs]:
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                first = first or e
    if first:
        raise first


def frozen_forms(project):
    """各题最新的冻结表单：{题号: 冻结事件的内容}。"""
    out = {}
    for r in project.q("SELECT ref_id, content FROM events WHERE type='表单冻结' ORDER BY seq"):
        out[r["ref_id"]] = json.loads(r["content"])
    return out


def form_candidates(project):
    out = {}
    for r in project.q("SELECT ref_id, content FROM events WHERE type='表单候选' ORDER BY seq"):
        out[r["ref_id"]] = json.loads(r["content"])
    return out


def auto_release_ok(cand):
    """自动放行的条件（全部满足才行）：表单阶段闭合；术语展开全是"相等"（闭合本身已要求各方一致、归类无差异、
    没有硬伤、程序逐字核对通过）。"""
    if not cand or cand.get("状态") != "闭合":
        return False, "表单阶段没有闭合（有争议），必须由人确认"
    bad = [t["term"] for t in (cand.get("候选") or {}).get("terms") or [] if t.get("range") != "相等"]
    if bad:
        return False, "术语展开有不是「相等」的：" + "、".join(bad)
    return True, ""


class _InstrHub:
    """2.9：在调用模型前，把当前位置已生效的用户指令附在提示词最前面。存档的调用照旧读存档（存档按调用编号取，不看提示词）。"""

    def __init__(self, hub, flow):
        self._hub, self._flow = hub, flow

    def __getattr__(self, name):
        return getattr(self._hub, name)

    def call_json(self, role, system, user, **kw):
        key = kw.get("call_key", "")
        # 2.9.1 直接打断：被切断的那一步里，只有已经存档的结果算数，作废的不补（另起编号的"-u"是从没答过的生产者第一次作答）
        if self._flow._in_cut_step() and not key.endswith("-u") and not self._flow._archived(key):
            raise I.Voided(key)
        ins = self._flow._active_instr()
        if ins and role in self._flow._instr_roles:
            user = I.block(ins) + user
        return self._hub.call_json(role, system, user, **kw)


class FormFlow(ReviewFlow):
    def __init__(self, project, settings, hub, retriever=None):
        super().__init__(project, settings, hub, retriever)
        roles = settings["roles"]
        self.faultfinders = [r for r, v in roles.items() if v.get("kind") == "找茬者"]
        params = {**settings["params"], **(self.cfg.get("params") or {})}
        self.form_cap = max(1, int(params.get("form_rounds", 5)))
        self.auto_release = bool(params.get("form_auto_release", False))
        self.summaries = {}  # (题号, 审查者, 轮次) -> {标签: 摘要文字}
        # 2.8：表单阶段开关。2.7 项目始终开；2.8 项目以账本里第一次记下的设置为准（运行中途改参数不影响已开始的项目）
        rec = self.p.q("SELECT content FROM events WHERE type='表单阶段设置' ORDER BY seq LIMIT 1")
        if self.cfg.get("flow_version") == "2.7":
            self.use_form, self._form_logged = True, True
        elif rec:
            self.use_form, self._form_logged = bool(json.loads(rec[0]["content"]).get("开启")), True
        else:
            self.use_form, self._form_logged = bool(params.get("form_stage", False)), False
        # 2.9：插话
        self.hub = _InstrHub(self.hub, self)
        self._instr_roles = set(self.producers) | set(self.reviewers) | set(self.faultfinders)
        self._load_turns()
        self._pos = None
        self._calls0 = 0

    # ================================================================ 2.9.1 插话、打断、转折点
    def _active_instr(self):
        if not self._pos:
            return []
        return [x for x in self.instr if x["pos"] <= self._pos]

    def _load_turns(self):
        """转折点：用户指令的位置，以及直接打断的位置（直接打断时可能一句话都没说，也要"先进审查者"，不补作废的调用）。"""
        self.instr = I.load(self.p)
        self.cuts = I.load_cuts(self.p)  # [(位置, 被切断的步骤)]
        self.turns = sorted({x["pos"] for x in self.instr} | {c[0] for c in self.cuts})

    def _archived(self, key):
        from .models import _like_escape
        return bool(self.p.q("SELECT 1 FROM calls WHERE call_id LIKE ? ESCAPE '\\' AND status='成功' LIMIT 1",
                             (_like_escape(key) + "#%",)))

    def _in_cut_step(self):
        return any(c[1] == self._pos for c in self.cuts)

    def _checkpoint(self, states, r, s):
        """每一步开始前：①送达运行中插的话（只在本次运行已经真的调用过模型之后——之前是在重放存档）；②到了转折点就按规则处理
        （重放时也要处理，结果与当时一样）；③有暂停请求就暂停。返回 True＝这一轮余下的步骤不做。"""
        self._pos = (r, s)
        live = self.p.q("SELECT COUNT(*) c FROM calls")[0]["c"] > self._calls0
        root = self.p.root
        if live and I.pending(root):
            I.ingest(self.p, root, (r, s))
            self._load_turns()
        skip = False
        if (r, s) in self.turns:
            skip = self._turn(states, r, s)
        if live and I.pause_requested(root):
            (root / I.FLAG).unlink(missing_ok=True)
            self.p.log("程序", "已暂停", round_=r, content={"位置": [r, s], "说明": f"按你的请求在{I.pos_text((r, s))}暂停。"
                                                                     "可以继续补充；补充完点「继续运行」，所有角色都会收到。"})
            raise I.Paused()
        return skip

    def _turn(self, states, r, s):
        ids = [x["id"] for x in self.instr if x["pos"] == (r, s)]
        first = r if s <= 2 else r + 1  # "先审"的那一轮
        for st in states:
            st.review_first = first
            st.cap = first + self.cap - 1
            st.turn_ids = sorted(set(getattr(st, "turn_ids", [])) | set(ids))
            if st.done:  # 已经结束的题也要重审：结论保留，重新打开交审查者
                st.prev_final = st.final
                st.done, st.final = False, None
                if first == r and s == 2:  # 这一轮的生产者那一步已经过去了：直接沿用已有答案
                    for p in self.producers:
                        self._producer(p, st, r, force_carry=True)
        self._log_once("程序", "转折点", f"转折|{r}|{s}", {
            "位置": [r, s], "用户指令": ids, "先审的一轮": first,
            "说明": f"从第 {first} 轮起先由审查者按「原题＋补充」审一遍已有的内容（含已闭合的题），再按审查结果返工"}, r)
        return s >= 3

    def _review_first_producer(self, role, st, r):
        """"先审"的那一轮：已存档的就用（不浪费）；手里有答案的沿用；一份都没有的直接作答。"""
        key = f"p-{role}-{st.id}-r{r}"
        if self._archived(key):
            return self._producer(role, st, r)
        if st.subs[role]:
            return self._producer(role, st, r, force_carry=True)
        return self._producer(role, st, r, fresh_key=f"{key}-u")

    def _decide(self, st, r):
        super()._decide(st, r)
        if st.done and st.final is not None and getattr(st, "turn_ids", None) is not None and getattr(st, "review_first", 0):
            st.final["用户补充后重审"] = {"用户指令": st.turn_ids, "先审的一轮": st.review_first,
                                    "之前的结论": (getattr(st, "prev_final", None) or {}).get("状态")}

    # ================================================================ 第一段：表单
    def _form_producer(self, role, st, r):
        iid, problem = st.id, st.it["problem"]
        hist = st.forms[role]
        ops = st.ops_to.get(role, []) if r > 1 else []
        if r > 1 and not ops:
            rec = dict(hist[-1], round=r, carried=True, responses=[])
            hist.append(rec)
            self._log_once(role, "表单提交", f"{iid}|{role}|{r}", dict(rec, item=iid, role=role), r)
            return
        if r == 1:
            obj = self.hub.call_json(role, S.SYSTEM_FORM, S.form_prompt(problem, self.lang),
                                     call_key=f"f-{role}-{iid}-r1", round_=r, validator=S.validate_form(problem),
                                     max_tokens=self.max_tokens)
        else:
            obj = self.hub.call_json(role, S.SYSTEM_FORM_REVISE, S.form_revise_prompt(problem, hist[-1]["form"], ops, self.lang),
                                     call_key=f"f-{role}-{iid}-r{r}", round_=r,
                                     validator=S.validate_form(problem, [o["id"] for o in ops]), max_tokens=self.max_tokens)
        form = {k: obj[k] for k in ("items", "terms", "implicit")}
        rec = {"round": r, "carried": False, "form": dict(form, check=obj["check"]), "responses": obj["responses"]}
        hist.append(rec)
        self._log_once(role, "表单提交", f"{iid}|{role}|{r}", dict(rec, item=iid, role=role), r)
        for resp in rec["responses"]:
            self.p.exec("UPDATE opinions SET status=?, evidence=? WHERE opinion_id=?",
                        (resp["decision"], resp["check"], resp["opinion"]))
            self._log_once(role, "意见回应", f"{resp['opinion']}|{r}", dict(resp, round=r, item=iid), r)

    def _forms_now(self, st):
        return {self.label[p]: st.forms[p][-1]["form"] for p in self.producers}

    def _form_reviewer(self, rv, st, r):
        iid, problem = st.id, st.it["problem"]
        forms = self._forms_now(st)
        labels = list(forms)
        a = self.hub.call_json(rv, S.SYSTEM_FORM_REVIEWER, S.form_review_prompt(problem, forms, st.diffs[r], self.lang),
                               call_key=f"fr-{rv}-{iid}-r{r}", round_=r,
                               validator=S.validate_form_review(labels, problem, forms), max_tokens=self.max_tokens)
        res = {"reviewer": rv, "overall": a["overall"], "ops": [], "terms_consistent": a["terms_consistent"],
               "implicit_consistent": a["implicit_consistent"]}
        self._log_once(rv, "表单评审", f"{iid}|{rv}|{r}", {"item": iid, "round": r, "总体评价": a["overall"],
                                                       "术语展开一致": a["terms_consistent"], "隐含项一致": a["implicit_consistent"],
                                                       "意见数": len(a["opinions"])}, r)
        self._add_ops(st, r, res, a["opinions"], tag="", event="表单意见", stage="表单")
        return res

    def _form_diffs(self, st, r):
        problem = st.it["problem"]
        res = {self.label[p]: FC.check_form(problem, st.forms[p][-1]["form"]) for p in self.producers}
        diffs = FC.label_disagreements(problem, res) if len(res) > 1 else []
        st.diffs[r] = diffs
        ops = []
        for d in diffs:
            for lab, field in d["各方归类"].items():
                ops.append({"target": lab, "severity": "次要", "basis": "", "layer": "表单", "location": d["原文"][:60],
                            "problem": f"题面原文「{d['原文']}」你归入了「{field}」，与其他表单的归类不同；请对照题面原文核实这一段"
                                       "应该归哪一栏（条件／范围／所求／完成标准／排除项），核实后采纳或驳回。", "internal_note": ""})
        res_prog = {"reviewer": "程序", "ops": []}
        self._add_ops(st, r, res_prog, ops, tag="p", event="表单意见", stage="表单", source="程序")
        st.prog_ops[r] = res_prog["ops"]
        if diffs:
            self._log_once("程序", "表单归类差异", f"{st.id}|{r}", {"item": st.id, "round": r, "差异": diffs}, r)

    def _form_evaluate(self, st, r):
        revs = st.reviews[r]
        hard = [o for rv in revs for o in rv["ops"] if o["severity"] == "硬伤"]
        bad = [(rv["reviewer"], l) for rv in revs for l, v in rv["overall"].items() if v != "通过"]
        diffs = st.diffs.get(r) or []
        cons = all(rv["terms_consistent"] == "是" and rv["implicit_consistent"] == "是" for rv in revs)
        closed = not hard and not bad and not diffs and cons
        stall = st.stall
        if r > 1:
            same = all(st.forms[p][-1]["carried"] or
                       {k: st.forms[p][-1]["form"][k] for k in ("items", "terms", "implicit")} ==
                       {k: st.forms[p][-2]["form"][k] for k in ("items", "terms", "implicit")} for p in self.producers)
            stall = st.stall + 1 if same else 0
        to_send = any(rv["ops"] for rv in revs) or bool(st.prog_ops.get(r))
        reason = None
        if not closed:
            if stall >= 2:
                reason = "停滞：连续两轮各方表单没有改动"
            elif not to_send:
                reason = "各方表单不一致（术语展开或隐含项），但没有可发给生产者的意见，继续迭代不会有变化"
            elif r >= self.form_cap:
                reason = f"达到表单阶段轮数上限（{self.form_cap} 轮）仍未一致"
        return {"closed": closed, "reason": reason, "stall": stall,
                "summary": {"item": st.id, "round": r, "硬伤意见数": len(hard), "归类差异数": len(diffs),
                            "未通过": [f"{a}:{l}" for a, l in bad], "术语与隐含项一致": cons, "闭合": closed, "停滞计数": stall}}

    def _form_decide(self, st, r):
        ev = self._form_evaluate(st, r)
        st.stall = ev["stall"]
        self._log_once("程序", "表单小结", f"{st.id}|{r}", ev["summary"], r)
        if ev["closed"] or ev["reason"]:
            st.done = True
            forms = self._forms_now(st)
            first = self.label[self.producers[0]]
            cand = {k: forms[first][k] for k in ("items", "terms", "implicit")}
            added = {lab: (f.get("check") or {}).get("added") or [] for lab, f in forms.items()}
            hints = {lab: (f.get("check") or {}).get("hints") or [] for lab, f in forms.items()}
            st.final = {"item": st.id, "状态": "闭合" if ev["closed"] else "有争议", "原因": ev["reason"], "轮数": r,
                        "候选": cand, "候选来自": f"生产者{first}（{self.producers[0]}）的表单",
                        "各方表单": {lab: {k: f[k] for k in ("items", "terms", "implicit")} for lab, f in forms.items()},
                        "补词（含模型判断）": added, "程序提示": hints, "题面": st.it["problem"]}
            self._log_once("程序", "表单候选", st.id, st.final, r)
            return
        st.ops_to = {p: [] for p in self.producers}
        for o in [o for rv in st.reviews[r] for o in rv["ops"]] + list(st.prog_ops.get(r) or []):
            st.ops_to[o["role"]].append({"id": o["id"], "severity": o["severity"], "basis": o.get("basis", ""),
                                         "problem": o["sent"]})

    def _form_stage(self):
        states = [_FormItem(it) for it in self.items]
        for st in states:
            st.forms = {p: [] for p in self.producers}
        for r in range(1, self.form_cap + 1):
            active = [s for s in states if not s.done]
            if not active:
                break
            _pool([(lambda s=s, p=p: self._form_producer(p, s, r)) for s in active for p in self.producers])
            for s in active:
                self._form_diffs(s, r)
            jobs = []
            for s in active:
                s.reviews[r] = [None] * len(self.reviewers)
                jobs += [(lambda s=s, i=i, rv=rv: s.reviews[r].__setitem__(i, self._form_reviewer(rv, s, r)))
                         for i, rv in enumerate(self.reviewers)]
            _pool(jobs)
            ff_jobs = []
            for s in active:
                ev = self._form_evaluate(s, r)
                if self._ff_appears(s, r, ev["closed"] or ev["reason"]):
                    ff_jobs.append(lambda s=s: self._ff_form(s, r))
                else:
                    self._log_once("程序", "找茬者未出场", f"表单|{s.id}|{r}", {"item": s.id, "round": r, "阶段": "表单",
                                                                        "说明": self._ff_absent_reason(s, r)}, r)
            if ff_jobs:
                _pool(ff_jobs)
            for s in active:
                self._form_decide(s, r)
        return states

    # ================================================================ 找茬者（两段共用）
    def _ff_appears(self, st, r, ends):
        if not self.faultfinders:
            return False
        return r == 1 or (r - 1) not in st.ff or bool(ends)

    def _ff_absent_reason(self, st, r):
        return "没有配置找茬者" if not self.faultfinders else "上一轮已出场，这一轮也不是最后一轮"

    def _ff_form(self, st, r):
        st.ff.add(r)
        problem = st.it["problem"]
        mats = {lab: S.form_text(f) for lab, f in self._forms_now(st).items()}
        header = f"题面原文：\n{problem}\n\n（这一步看的是各生产者把题面拆成的表单：找它们是否共同漏了、加了、改了同一处，或彼此冲突。）"
        issues = self._ff_call(st, r, header, mats, "表单")
        if issues:
            ctx = "\n\n".join(S.form_text(f, lab) for lab, f in self._forms_now(st).items())
            self._recheck(st, r, header, issues, lambda rv: ctx, list(mats), FORM_REVIEW_BASES, "表单意见", "表单")

    def _ff_call(self, st, r, header, mats, stage):
        out = []
        for F in self.faultfinders:
            key = f"{'ff' if stage == '表单' else 'fc'}-{F}-{st.id}-r{r}"
            a = self.hub.call_json(F, S.SYSTEM_FAULTFINDER, S.faultfinder_prompt(header, mats, self.lang),
                                   call_key=key, round_=r, validator=S.validate_faultfinder(mats), max_tokens=self.max_tokens)
            self._log_once(F, "找茬", f"{stage}|{st.id}|{F}|{r}", {"item": st.id, "round": r, "阶段": stage,
                                                                "问题": a["issues"]}, r)
            out += a["issues"]
        return out

    def _recheck(self, st, r, header, issues, ctx_of, labels, bases, event, stage):
        def one(i, rv):
            a = self.hub.call_json(rv, S.SYSTEM_RECHECK, S.recheck_prompt(header, issues, ctx_of(rv), labels, self.lang),
                                   call_key=f"{'frc' if stage == '表单' else 'rc'}-{rv}-{st.id}-r{r}", round_=r,
                                   validator=S.validate_recheck(labels, len(issues), bases), max_tokens=self.max_tokens)
            self._log_once(rv, "复查", f"{stage}|{st.id}|{rv}|{r}", {"item": st.id, "round": r, "阶段": stage,
                                                                 "结论": a["decisions"]}, r)
            adopted = [d["opinion"] for d in a["decisions"] if d["decision"] == "采纳" and d["opinion"]]
            res = st.reviews[r][i]
            self._add_ops(st, r, res, adopted, tag="f", event=event, stage=stage, source="找茬者问题（审查者复查后采纳）")
        _pool([(lambda i=i, rv=rv: one(i, rv)) for i, rv in enumerate(self.reviewers)])

    def _add_ops(self, st, r, res, ops, tag, event, stage, source=None):
        """把一批意见记进账本并挂到 res["ops"] 上（编号、抹去他人信息、意见表、事件）。"""
        iid = st.id
        rv = res["reviewer"]
        res.setdefault("ops", [])
        start = len([o for o in res["ops"] if o.get("tag") == tag])
        for n, o in enumerate(ops, start + 1):
            pre = "F" if stage == "表单" else "r"
            oid = f"{iid}.{pre}{r}.{rv}.{tag}{n}"
            role = self.by_label[o["target"]]
            others_lab = [self.label[x] for x in self.producers if x != role]
            others_ans = [] if stage == "表单" else [st.subs[x][-1]["answer"] for x in self.producers if x != role]
            text, k = T.mask_foreign(o["problem"], others_ans, others_lab)
            o = dict(o, id=oid, role=role, sent=text, masked=k, tag=tag)
            res["ops"].append(o)
            self.op_text[oid] = text
            if not self.p.q("SELECT 1 FROM opinions WHERE opinion_id=?", (oid,)):
                self.p.exec("INSERT INTO opinions (opinion_id,source_role,review_id,target_author,item_id,content,status,"
                            "closed_round,evidence,kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (oid, rv, f"{iid}|{rv}|{r}", role, iid, json.dumps(o, ensure_ascii=False), "提出", None, None,
                             o.get("layer") or stage))
            self._log_once(rv, event, oid, {"item": iid, "round": r, "对象": role, "层": o.get("layer"), "阶段": stage,
                                            "严重程度": o["severity"], "被反驳的依据": o.get("basis") or None,
                                            "位置": o.get("location"), "发出的意见": text, "原文": o["problem"],
                                            "来源": source or "审查者", "抹去他人信息处数": k}, r)

    # ================================================================ 冻结
    def _ensure_frozen(self, fstates):
        """返回 {题号: 冻结的表单}；没冻结的：开了自动放行且条件满足就自动冻结，否则留给人。"""
        fro = frozen_forms(self.p)
        for st in fstates:
            if st.id in fro:
                continue
            ok, why = auto_release_ok(st.final)
            if self.auto_release and ok:
                self.p.log("程序", "表单自动放行", ref_type="key", ref_id=st.id,
                           content={"item": st.id, "说明": "参数面板开了自动放行，且条件全部满足"})
                self.p.log("程序", "表单冻结", ref_type="key", ref_id=st.id,
                           content={"item": st.id, "版本": 1, "来源": "自动放行", "模型起草版": st.final["候选"],
                                    "冻结版": st.final["候选"], "用户改动": []})
            elif self.auto_release:
                self._log_once("程序", "表单不能自动放行", st.id, {"item": st.id, "原因": why})
        return {k: v["冻结版"] for k, v in frozen_forms(self.p).items()}

    # ================================================================ 第二段：做题
    def _review_call(self, rv, st, r, labels, facts):
        it = st.it
        form = it.get("_form")
        raw = form is None  # 2.8：表单阶段关闭，按原题审
        if raw:
            excl, req_ids = [], [S.RAW_REQ]
        else:
            rq = FC.requirements(form)
            excl = FC.excluded_ids(form)
            req_ids = [x["id"] for x in rq if x["id"] not in excl]
        views = {v["label"]: v for v in self._views(st, r)}
        per = {}

        def one(lab):
            v = views[lab]
            paper = S.paper_text(v)
            a = self.hub.call_json(rv, S.SYSTEM_REVIEW_ONE_RAW if raw else S.SYSTEM_REVIEW_ONE,
                                   S.review_one_prompt(it["problem"], self._scope(it), v, req_ids, self.libs, self.allow_code,
                                                       self.lang, raw=raw),
                                   call_key=f"rv1-{rv}-{st.id}-{lab}-r{r}", round_=r,
                                   validator=S.validate_review_one(lab, req_ids, excl, paper, "原题" if raw else "表单"),
                                   max_tokens=self.max_tokens)
            per[lab] = a
            self._log_once(rv, "逐份评审", f"{st.id}|{rv}|{lab}|{r}", {
                "item": st.id, "round": r, "对象": lab, "总体评价": a["overall"], "逐条结论": a["requirements"],
                "额外前提": a["extra_premises"], "摘要": a["summary"], "超出口径（按次要处理）": a["out_of_scope"],
                "审查者越界（自己填了答案）": a["own_answer"] or None}, r)
        _pool([(lambda lab=lab: one(lab)) for lab in labels])
        ops, impls, ups = [], [], []
        for lab in labels:
            ops += per[lab]["opinions"]
            impls += per[lab]["implementations"]
            ups += per[lab]["unverified_parts"]
        same_route, eq = False, "不确定"
        if len(labels) >= 2:
            sums = {lab: S.summary_text(lab, views[lab]["status"], views[lab]["answer"], per[lab]["summary"]) for lab in labels}
            self.summaries[(st.id, rv, r)] = sums
            c = self.hub.call_json(rv, S.SYSTEM_COMPARE, S.compare_prompt(it["problem"], sums, facts, self.lang),
                                   call_key=f"rvc-{rv}-{st.id}-r{r}", round_=r, validator=S.validate_compare(sums),
                                   max_tokens=self.max_tokens)
            same_route, eq = c["same_route"], c["answers_equivalent"]
            self._log_once(rv, "对照审查", f"{st.id}|{rv}|{r}", {"item": st.id, "round": r, "材料间冲突": c["conflicts"],
                                                            "路线相同": same_route, "答案等价": eq}, r)
            for cf in c["conflicts"]:
                for t in cf["targets"]:
                    mine = cf["quote_a"] if t == cf["a"] else cf["quote_b"]
                    other = cf["quote_b"] if t == cf["a"] else cf["quote_a"]
                    where = "你的材料里另一处" if cf["a"] == cf["b"] else "另一份材料里"
                    ops.append({"target": t, "severity": cf["severity"] if cf["severity"] == "硬伤" else "次要",
                                "basis": "自相矛盾" if cf["severity"] == "硬伤" else "", "requirement": "",
                                "layer": "材料间冲突", "location": "对照",
                                "problem": f"材料之间有冲突：{cf['explain']}。你的材料里写的是「{mine}」，{where}是「{other}」。请核实后采纳或驳回。",
                                "internal_note": ""})
        else:
            self.summaries[(st.id, rv, r)] = {lab: S.summary_text(lab, views[lab]["status"], views[lab]["answer"],
                                                                  per[lab]["summary"]) for lab in labels}
        return {"overall": {lab: per[lab]["overall"] for lab in labels}, "opinions": ops, "implementations": impls,
                "unverified_parts": ups, "same_route": same_route, "answers_equivalent": eq, "fallback_answer": ""}

    def _ff_solve(self, st, r):
        st.ff.add(r)
        views = self._views(st, r)
        mats = {v["label"]: S.paper_text(v) for v in views}
        header = st.it["problem"] + "\n" + T.scope_block(self._scope(st.it))
        issues = self._ff_call(st, r, header, mats, "做题")
        if issues:
            labels = list(mats)

            def ctx(rv):
                return "\n".join((self.summaries.get((st.id, rv, r)) or {}).values()) or "（没有摘要）"
            self._recheck(st, r, header, issues, ctx, labels, SOLVE_RECHECK_BASES, "评审意见", "做题")

    def _solve_stage(self, frozen):
        """frozen 为 None＝2.8 表单阶段关闭：直接用原题和口径。"""
        states = []
        for it in self.items:
            if frozen is None:
                view = dict(it)
            else:
                form = frozen[it["id"]]
                view = dict(it, problem=FC.render(form), scope=FC.scope_text(form), _form=form)
            s = _Item(view)
            s.subs = {p: [] for p in self.producers}
            s.ff = set()
            states.append(s)
        r = 0
        while True:
            r += 1
            # 先过检查点：全部题都结束了，但还有刚送达的补充时，转折点会把已结束的题重新打开交审查者重审
            skip = self._checkpoint(states, r, 1)
            if not [s for s in states if not s.done]:
                break
            if r > max([getattr(s, "cap", self.cap) for s in states] + [self.cap]):
                break
            active = [s for s in states if not s.done]
            jobs = []
            for s in active:
                for p in self.producers:
                    if getattr(s, "review_first", 0) == r:
                        jobs.append(lambda s=s, p=p: self._review_first_producer(p, s, r))
                    else:
                        jobs.append(lambda s=s, p=p: self._producer(p, s, r))
            self._cut_pool(jobs)
            skip = self._checkpoint(states, r, 2) or skip
            active = [s for s in states if not s.done]  # 转折点可能重新打开了已结束的题
            jobs = []
            for s in active:
                s.reviews[r] = [None] * len(self.reviewers)
                jobs += [(lambda s=s, i=i, rv=rv: s.reviews[r].__setitem__(i, self._reviewer(rv, s, r)))
                         for i, rv in enumerate(self.reviewers)]
            self._cut_pool(jobs)
            if self._checkpoint(states, r, 3) or skip:
                continue
            ff_jobs = []
            for s in active:
                ev = self._evaluate(s, r)
                if self._ff_appears(s, r, ev["closed"] or ev["reason"]):
                    ff_jobs.append(lambda s=s: self._ff_solve(s, r))
                else:
                    self._log_once("程序", "找茬者未出场", f"做题|{s.id}|{r}", {"item": s.id, "round": r, "阶段": "做题",
                                                                        "说明": self._ff_absent_reason(s, r)}, r)
            if ff_jobs:
                self._cut_pool(ff_jobs)
            if self._checkpoint(states, r, 4):
                continue
            for s in active:
                self._decide(s, r)
        for s in states:
            if not s.done:
                s.final = self._final(s, self.cap, False, None, "未知原因未闭合")
                s.done = True
        return states

    def _cut_pool(self, jobs):
        """直接打断切断的那一步：作废的调用不补，这一步余下的结果不用（转折点会让这一轮余下的步骤跳过）。"""
        try:
            _pool(jobs)
        except I.Voided:
            if not self._in_cut_step():
                raise

    # ================================================================ 主流程
    def run(self):
        if not self.p.q("SELECT 1 FROM events WHERE type='准入判断'"):
            self.p.log("用户", "准入判断", content={"结果": "强制放行", "说明": "由用户在建题窗口或手动放行"})
        if self.is_finished():
            return "已结束"
        self._calls0 = self.p.q("SELECT COUNT(*) c FROM calls")[0]["c"]
        pp = I.last_pause(self.p)
        if pp is not None:
            self.p.log("用户", "继续运行", content={"位置": list(pp), "说明": f"从{I.pos_text(pp)}接着做；暂停期间补充的内容所有角色都会收到"})
        try:
            self._setup_env()
            if not self._form_logged:
                self._log_once("程序", "表单阶段设置", "表单阶段", {
                    "开启": self.use_form,
                    "说明": ("开启：先拆表单、确认冻结，再只依据表单做题（与 2.7 相同）" if self.use_form else
                             "关闭：不拆表单，生产者、审查者、找茬者都直接看原题和口径；生产者第一轮的结果就是基线")
                    + "。本项目以这次的设置为准，之后改参数不影响本项目。"})
                self._form_logged = True
            if self.use_form:
                fstates = self._form_stage()
                frozen = self._ensure_frozen(fstates)
                wait = [s.id for s in fstates if s.id not in frozen]
                if wait:
                    sig = "|".join(wait)
                    self._log_once("程序", "等待确认表单", f"等待|{sig}", {"题号": wait,
                                                                     "说明": "表单阶段已结束，请在窗口里点「确认表单」逐题确认或修改后确认；全部确认后点「继续运行」开始做题"})
                    return "等待确认表单：" + "、".join(wait)
            else:
                frozen = None
            states = self._solve_stage(frozen)
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
        except I.Paused:
            return "已暂停：等你补充。补充完点「继续运行」。"
        left = I.pending(self.p.root)
        if left:  # 流程已经全部结束，下一个检查点没有到来：如实记下没送达
            self.p.log("程序", "插话未送达", content={"内容": [m["text"] for m in left],
                                                 "说明": "流程在下一个检查点之前已经全部结束，这些话没有送达任何角色"})
            for m in left:
                (self.p.root / I.INBOX / m["file"]).unlink(missing_ok=True)
        (self.p.root / I.FLAG).unlink(missing_ok=True)
        self.p.log("程序", "迭代结束", content={"原因": "全部题目已闭合或已标为有争议"})
        return "完成"


def confirm_form(project, iid, form, note=""):
    """用户确认（或修改后确认）一道题的表单：冻结并留痕（模型起草版、用户改动、冻结版）。做题开始后不能再改。"""
    cands = form_candidates(project)
    if iid not in cands:
        raise ValueError("这道题的表单阶段还没结束")
    if project.q("SELECT 1 FROM events WHERE type='提交' AND ref_id LIKE ?", (f"{iid}|%",)):
        raise ValueError("这道题已经开始做题，表单不能再改（2.7 不支持冻结后改表单；以后的版本做成开新版本）")
    clean = S._clean_form(form)
    draft = cands[iid]["候选"]
    chk = FC.check_form(cands[iid]["题面"], clean)
    ver = len(project.q("SELECT 1 FROM events WHERE type='表单冻结' AND ref_id=?", (iid,))) + 1
    diff = FC.diff_forms(draft, clean)
    project.log("用户", "表单冻结", ref_type="key", ref_id=iid,
                content={"item": iid, "版本": ver, "来源": "用户确认" + ("（有修改）" if diff else "（未修改）"),
                         "模型起草版": draft, "冻结版": clean, "用户改动": diff,
                         "用户改写后不在原文里的地方": chk["errors"], "说明": str(note or "")[:500]})
    return {"版本": ver, "改动": len(diff), "不在原文里": chk["errors"]}
