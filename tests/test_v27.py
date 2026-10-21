"""2.7：表单阶段（只拆不改写、逐字核对、术语展开、隐含项、放行与冻结）、做题阶段只依据表单、
分开审再对照、反驳够用即止、找茬者出场规则与复查、裸题对照、报告新统计、配置面板、豆包上限、回归题。"""

import _winfix  # noqa: F401  Windows：测试结束时清理临时文件夹（见 _winfix.py）
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from alreadythere import feed, formcheck as FC, report
from alreadythere.flow_form import FormFlow, confirm_form, frozen_forms
from alreadythere.models import MockProvider, ModelHub
from alreadythere.store import Project
from alreadythere.tasks import stage27 as S

ROOT = Path(__file__).resolve().parent.parent

Q1 = (r"For integers \(k, \ell \ge 2\), let \(m(k, \ell)\) denote the maximum of \(|\mathcal{F}|\,|\mathcal{G}|\), "
      r"where the maximum is taken over all pairs of cross-intersecting families, \(\mathcal{F}\) being a k-uniform "
      r"hypergraph with covering number \(\ell\), and \(\mathcal{G}\) an \(\ell\)-uniform hypergraph with covering number k. "
      r"What is the exact value of \(m(k,k)\) as a function of k, for sufficiently large k?")

GOOD_FORM = {"items": [
    {"field": "条件", "text": "For integers k, ℓ ≥ 2"},
    {"field": "所求", "text": r"m(k, \ell) denote the maximum of |F||G|"},
    {"field": "范围", "text": "the maximum is taken over all pairs of cross-intersecting families"},
    {"field": "条件", "text": "F being a k-uniform hypergraph with covering number ℓ"},
    {"field": "条件", "text": "【being】G an ℓ-uniform hypergraph with covering number k"},
    {"field": "所求", "text": "the exact value of m(k,k) as a function of k"},
    {"field": "范围", "text": "for sufficiently large k"}],
    "terms": [{"term": "covering number", "definition": "最少要几个点才能碰到每条边", "compute": "τ", "range": "相等"}],
    "implicit": [{"field": "排除项", "content": "不要求给出证明", "reason": "题面只问 exact value"}]}

ITEM = {"id": "Q1", "name": "q1", "problem": Q1, "gold": "(k^{k-1}+k-1)^{2}"}


class Env27:
    """可编程的模拟环境：按系统提示词区分是哪一步。各钩子返回要覆盖默认值的字典。"""

    def __init__(self, tmp, items=(ITEM,), producers=("P1", "P2"), reviewers=("R",), ff=("F",), params=None, hooks=None,
                 flow_version="2.7"):
        cfg = {"project_id": "T", "question": "测试", "scope": "测试", "task": "final_answer", "flow_version": flow_version,
               "items": list(items)}
        self.p = Project.create(Path(tmp) / "p", cfg)
        roles = {}
        for r in producers:
            roles[r] = {"kind": "生产者", "provider": "mock", "model": "m" + r, "series": "S" + r}
        for r in reviewers:
            roles[r] = {"kind": "审查者", "provider": "mock", "model": "m" + r, "series": "S" + r}
        for r in ff:
            roles[r] = {"kind": "找茬者", "provider": "mock", "model": "m" + r, "series": "S" + r}
        pr = {"retry_batch1": [0], "retry_batch2": [], "call_timeout_seconds": 5, "max_concurrency": 3,
              "allow_code_run": False, "review_rounds": 5, "form_rounds": 5}
        pr.update(params or {})
        self.st = {"roles": roles, "params": pr}
        self.hooks = hooks or {}
        self.prompts = []
        self.hub = ModelHub(self.p, self.st, {}, providers={"mock||": MockProvider(self._respond)}, sleep=lambda s: None)

    def flow(self):
        return FormFlow(self.p, self.st, self.hub)

    def _h(self, name, *a):
        f = self.hooks.get(name)
        return (f(*a) or {}) if f else {}

    def _respond(self, model, system, user):
        role = model[1:]
        self.prompts.append((role, system, user))
        n = sum(1 for r, s, _ in self.prompts if r == role and s == system)
        if system.startswith(S.SYSTEM_FORM[:30]) and "【生产者】" in system:
            ids = re.findall(r"意见编号：(\S+)", user)
            d = json.loads(json.dumps(GOOD_FORM))
            if ids:
                d["responses"] = [{"opinion": i, "decision": "采纳", "check": "对照原文核实"} for i in ids]
            d.update(self._h("form", role, n, user))
            return json.dumps(d, ensure_ascii=False)
        if system == S.SYSTEM_FORM_REVIEWER:
            labels = re.findall(r"【生产者(.)的表单】", user)
            d = {"overall": {l: "通过" for l in labels}, "opinions": [], "terms_consistent": "是", "implicit_consistent": "是"}
            d.update(self._h("form_review", role, n, user, labels))
            return json.dumps(d, ensure_ascii=False)
        if system == S.SYSTEM_FAULTFINDER:
            d = {"issues": []}
            d.update(self._h("ff", role, n, user))
            return json.dumps(d, ensure_ascii=False)
        if system == S.SYSTEM_RECHECK:
            k = len(re.findall(r"^\d+\. 〔", user, re.M))
            d = {"decisions": [{"issue": str(i), "decision": "驳回", "reason": "不成立"} for i in range(1, k + 1)]}
            d.update(self._h("recheck", role, n, user))
            return json.dumps(d, ensure_ascii=False)
        if system in (S.SYSTEM_REVIEW_ONE, S.SYSTEM_REVIEW_ONE_RAW):
            lab = re.search(r"【答卷(.)】", user).group(1)
            reqs = re.findall(r'"req": "([^"]+)"', user)
            d = {"overall": "通过", "requirements": [{"req": q, "verdict": "满足"} for q in reqs], "opinions": [],
                 "extra_premises": [], "unverified_parts": [], "implementation": None,
                 "summary": {"answer": "x", "claims": ["推导"], "theorems": [], "examples": [], "route": "构造"}, "own_answer": ""}
            d.update(self._h("one", role, n, user, lab))
            return json.dumps(d, ensure_ascii=False)
        if system == S.SYSTEM_COMPARE:
            d = {"conflicts": [], "same_route": False, "answers_equivalent": "不确定"}
            d.update(self._h("compare", role, n, user))
            return json.dumps(d, ensure_ascii=False)
        if system == S.SYSTEM_BARE:
            return "想了一下。\n最终答案：(k^{k-1}+k-1)^2"
        if "【生产者】" in system:  # 做题
            ids = re.findall(r"意见编号：(\S+)", user)
            d = {"status": "得出", "answer": "(k^{k-1}+k-1)^{2}", "reasoning": "推导",
                 "verify_plan": {"what": "w", "method": "m", "expected": "e", "tolerance": "无"}, "code": "", "limits": ""}
            if ids:
                d["responses"] = [{"opinion": i, "decision": "驳回", "check": "我重新检查了，原做法没有问题。"} for i in ids]
                d["changes"] = []
            d.update(self._h("solve", role, n, user))
            return json.dumps(d, ensure_ascii=False)
        labels = re.findall(r"【生产者(.)】", user)
        return json.dumps({"verifications": [{"target": l, "result": "一致", "diagnosis": "无", "reason": "r"} for l in labels],
                           "opinions": [], "unverified": []}, ensure_ascii=False)

    def ev(self, t):
        return [json.loads(r["content"]) for r in self.p.q("SELECT content FROM events WHERE type=? ORDER BY seq", (t,))]

    def calls(self, system):
        return [(r, u) for r, s, u in self.prompts if s == system]


def run_to_end(e):
    r = e.flow().run()
    if r.startswith("等待确认表单"):
        for iid, c in __import__("alreadythere.flow_form", fromlist=["x"]).form_candidates(e.p).items():
            if iid not in frozen_forms(e.p):
                confirm_form(e.p, iid, c["候选"])
        r = e.flow().run()
    return r


# ---------------------------------------------------------------- 逐字核对（规则 1–5）

class TestVerbatim(unittest.TestCase):
    def test_good_form_passes_with_added_word_listed(self):
        r = FC.check_form(Q1, GOOD_FORM)
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["added"], [{"项": 5, "补词": "being"}])

    def test_writing_variants_accepted(self):
        f = json.loads(json.dumps(GOOD_FORM))
        f["items"][0]["text"] = r"For integers \(k, \ell \geq 2\)"
        f["items"][3]["text"] = r"\mathcal{F} being a k-uniform hypergraph with covering number \ell"
        self.assertTrue(FC.check_form(Q1, f)["ok"])

    def test_no_fuzzy_match(self):
        """回归题①：DeepSeek 三次把"所有族对"读成"固定底集"——这类改写必须被拦下。"""
        f = json.loads(json.dumps(GOOD_FORM))
        f["items"][2]["text"] = "the maximum is taken over all pairs of cross-intersecting families on a fixed n-element ground set"
        r = FC.check_form(Q1, f)
        self.assertFalse(r["ok"])
        self.assertIn("找不到", r["errors"][0])

    def test_dropping_quantifier_is_missing(self):
        f = json.loads(json.dumps(GOOD_FORM))
        f["items"] = [x for x in f["items"] if "sufficiently" not in x["text"]]
        r = FC.check_form(Q1, f)
        self.assertFalse(r["ok"])
        self.assertTrue(any("sufficiently" in e and "关键词" in e for e in r["errors"]))

    def test_whole_problem_copied_gets_hint(self):
        r = FC.check_form(Q1, {"items": [{"field": "条件", "text": Q1}]})
        self.assertTrue(r["ok"])
        self.assertTrue(r["hints"])  # 只提示"可能没拆开"，不拦下

    def test_glue_lists_disjoint(self):
        self.assertFalse(set(FC.GLUE_EN) & set(FC.PROTECTED_EN))

    def test_label_disagreement(self):
        f2 = json.loads(json.dumps(GOOD_FORM))
        f2["items"][6]["field"] = "条件"
        res = {"甲": FC.check_form(Q1, GOOD_FORM), "乙": FC.check_form(Q1, f2)}
        d = FC.label_disagreements(Q1, res)
        self.assertEqual(len(d), 1)
        self.assertIn("sufficiently", d[0]["原文"])


# ---------------------------------------------------------------- 表单阶段

class TestFormStage(unittest.TestCase):
    def test_waits_for_human_then_solves_on_form_only(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t)
            self.assertTrue(e.flow().run().startswith("等待确认表单"))
            self.assertEqual(e.ev("表单候选")[0]["状态"], "闭合")
            self.assertFalse(e.ev("提交"))  # 冻结前不做题
            confirm_form(e.p, "Q1", GOOD_FORM)
            self.assertEqual(e.flow().run(), "完成")
            solve = [u for r, s, u in e.prompts if "【生产者】" in s and "不做题" not in s]
            self.assertTrue(solve)
            self.assertFalse(any(r"\mathcal{G}|" in u or "What is the exact value" in u for u in solve))  # 不给原题
            self.assertTrue(all("题目表单（已冻结" in u for u in solve))
            self.assertEqual(e.ev("最终状态")[0]["状态"], "闭合")

    def test_user_edit_recorded(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t)
            e.flow().run()
            f = json.loads(json.dumps(GOOD_FORM))
            f["items"][0]["text"] = "整数 k, l 至少为 2"
            info = confirm_form(e.p, "Q1", f)
            self.assertEqual(info["改动"], 1)
            fz = e.ev("表单冻结")[0]
            self.assertIn("有修改", fz["来源"])
            self.assertTrue(fz["用户改写后不在原文里的地方"])
            self.assertEqual(fz["模型起草版"]["items"][0]["text"], GOOD_FORM["items"][0]["text"])

    def test_auto_release(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True})
            self.assertEqual(e.flow().run(), "完成")
            self.assertEqual(e.ev("表单冻结")[0]["来源"], "自动放行")

    def test_auto_release_refused_when_term_narrowed(self):
        def narrow(role, n, user):
            f = json.loads(json.dumps(GOOD_FORM))
            f["terms"][0].update(range="更窄", lost="只算一部分")
            return f
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True}, hooks={"form": narrow})
            self.assertTrue(e.flow().run().startswith("等待确认表单"))
            self.assertIn("不是「相等」", e.ev("表单不能自动放行")[0]["原因"])

    def test_verbatim_failure_is_format_retry(self):
        def bad_first(role, n, user):
            if n == 1 and role == "P1":
                f = json.loads(json.dumps(GOOD_FORM))
                f["items"][2]["text"] = "pairs on a fixed ground set"
                return f
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, hooks={"form": bad_first})
            e.flow().run()
            fails = e.p.q("SELECT failure FROM calls WHERE role='P1' AND status='失败'")
            self.assertTrue(fails and "逐字核对" in fails[0]["failure"])
            self.assertEqual(e.ev("表单候选")[0]["状态"], "闭合")

    def test_disagreement_blocks_until_fixed(self):
        def p2(role, n, user):
            if role == "P2" and "意见编号" not in user:
                f = json.loads(json.dumps(GOOD_FORM))
                f["items"][6]["field"] = "条件"
                return f
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, hooks={"form": p2})
            e.flow().run()
            c = e.ev("表单候选")[0]
            self.assertEqual((c["状态"], c["轮数"]), ("闭合", 2))
            self.assertTrue(e.ev("表单归类差异"))
            self.assertTrue(any(x["来源"] == "程序" for x in e.ev("表单意见")))

    def test_inconsistent_implicit_goes_disputed_to_human(self):
        def rev(role, n, user, labels):
            return {"implicit_consistent": "否"}
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, hooks={"form_review": rev}, params={"form_auto_release": True})
            self.assertTrue(e.flow().run().startswith("等待确认表单"))
            self.assertEqual(e.ev("表单候选")[0]["状态"], "有争议")

    def test_form_rounds_default_five(self):
        from alreadythere.config import DEFAULT_PARAMS
        self.assertEqual(DEFAULT_PARAMS["form_rounds"], 5)
        self.assertFalse(DEFAULT_PARAMS["form_auto_release"])


# ---------------------------------------------------------------- 找茬者出场规则

class TestFaultfinder(unittest.TestCase):
    def _rounds(self, e, stage):
        return sorted(x["round"] for x in e.ev("找茬") if x["阶段"] == stage)

    def test_schedule_and_final_round(self):
        cnt = []

        def solve(role, n, user):  # 让做题阶段跑 3 轮才一致
            if role == "P2":
                cnt.append(1)
                if len(cnt) <= 2:
                    return {"answer": f"{len(cnt)}"}

        def one(role, n, user, lab):
            if lab == "乙" and "答案：1" in user or lab == "乙" and "答案：2" in user:
                return {"overall": "有问题", "opinions": [{"severity": "硬伤", "basis": "计算错误", "problem": "第二步算错"}]}
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True}, hooks={"solve": solve, "one": one})
            self.assertEqual(e.flow().run(), "完成")
            self.assertEqual(self._rounds(e, "表单"), [1])  # 第一轮必出场（这一轮就闭合，也是最后一轮）
            self.assertEqual(self._rounds(e, "做题"), [1, 3])  # 第 1 轮必出；第 2 轮上一轮出过且不是最后；第 3 轮将结束
            absent = [x for x in e.ev("找茬者未出场") if x["阶段"] == "做题"]
            self.assertEqual([x["round"] for x in absent], [2])

    def test_ff_issue_adopted_revokes_closure(self):
        def ff(role, n, user):
            if "【生产者甲】" in user and "答卷" not in user and "推导" in user and n == 2:
                return {"issues": [{"type": "共同前提", "targets": ["甲", "乙"], "quotes": [{"label": "甲", "text": "推导"}],
                                    "content": "两方都没说明前提"}]}

        def rc(role, n, user):
            if n == 1:
                return {"decisions": [{"issue": "1", "decision": "采纳", "reason": "成立",
                                       "opinion": {"target": "甲", "severity": "硬伤", "basis": "反例", "problem": "有反例 X"}}]}
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True}, hooks={"ff": ff, "recheck": rc})
            self.assertEqual(e.flow().run(), "完成")
            f = e.ev("最终状态")[0]
            self.assertEqual(f["轮数"], 2)  # 第 1 轮本来闭合，复查采纳了硬伤 → 撤销闭合，进第 2 轮
            adopted = [x for x in e.ev("评审意见") if x.get("来源", "").startswith("找茬者")]
            self.assertEqual(len(adopted), 1)
            # 找茬者的意见不直接给生产者：生产者看到的是审查者的意见，不注明来源
            rev = [u for r, s, u in e.prompts if r == "P1" and "修改轮" in s and "【生产者】" in s]
            self.assertTrue(rev and "有反例 X" in rev[0] and "找茬" not in rev[0])

    def test_no_faultfinder_configured(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, ff=(), params={"form_auto_release": True})
            self.assertEqual(e.flow().run(), "完成")
            self.assertFalse(e.ev("找茬"))
            self.assertTrue(all(x["说明"] == "没有配置找茬者" for x in e.ev("找茬者未出场")))


# ---------------------------------------------------------------- 分开审、再对照；逐条审查

class TestSplitReview(unittest.TestCase):
    def test_one_call_per_paper_plus_compare(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, producers=("P1", "P2", "P3"), reviewers=("R1", "R2"), params={"form_auto_release": True})
            self.assertEqual(e.flow().run(), "完成")
            ones = e.calls(S.SYSTEM_REVIEW_ONE)
            self.assertEqual(len(ones), 6)  # 2 个审查者 × 3 份答卷
            self.assertTrue(all(len(re.findall(r"【答卷.】", u)) == 1 for _, u in ones))  # 每次只看一份
            self.assertEqual(len(e.calls(S.SYSTEM_COMPARE)), 2)
            self.assertTrue(all("【答卷" in u and "推导：" not in u for _, u in e.calls(S.SYSTEM_COMPARE)))  # 对照只看摘要

    def test_summary_quotes_must_be_verbatim(self):
        def one(role, n, user, lab):
            if n == 1:
                return {"summary": {"answer": "x", "claims": ["这句话答卷里没有"], "route": "r"}}
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True}, hooks={"one": one})
            e.flow().run()
            fails = e.p.q("SELECT failure FROM calls WHERE role='R' AND status='失败'")
            self.assertTrue(fails and "逐字引自答卷原文" in fails[0]["failure"])

    def test_unsatisfied_requires_refutation(self):
        def one(role, n, user, lab):
            if n == 1:
                reqs = re.findall(r'"req": "(\d+)"', user)
                return {"requirements": [{"req": q, "verdict": "不满足" if q == reqs[0] else "满足"} for q in reqs]}
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True}, hooks={"one": one})
            e.flow().run()
            fails = e.p.q("SELECT failure FROM calls WHERE role='R' AND status='失败'")
            self.assertTrue(fails and "不满足必须附反驳" in fails[0]["failure"])

    def test_regression_overstrict_exclusion_not_blocking(self):
        """回归题②（2.4 过严）：审查者拿"没证明上界"当硬伤，而那正是排除项——程序判为超出口径，不挡闭合。"""
        def one(role, n, user, lab):
            excl = re.search(r"第(\d+)条〔排除项", user).group(1)
            return {"opinions": [{"severity": "硬伤", "basis": "与口径冲突", "requirement": excl, "problem": "没有证明上界"}]}
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True}, hooks={"one": one})
            self.assertEqual(e.flow().run(), "完成")
            f = e.ev("最终状态")[0]
            self.assertEqual((f["状态"], f["轮数"]), ("闭合", 1))
            self.assertTrue(e.ev("逐份评审")[0]["超出口径（按次要处理）"])

    def test_regression_overloose_conflict_blocks(self):
        """回归题③（2.5 过松）：25 > 24 那种"一方的例子推翻另一方引用的上界"，对照时列出就成为硬伤，挡闭合。"""
        def solve(role, n, user):
            if n == 1:
                return {"reasoning": "引用上界：乘积不超过 24" if role == "P1" else "例子：k=2、ℓ=3 时乘积是 25"}

        def one(role, n, user, lab):
            if "乘积不超过 24" in user:
                return {"summary": {"answer": "x", "theorems": [{"name": "上界", "quote": "乘积不超过 24"}], "route": "r"}}
            if "乘积是 25" in user:
                return {"summary": {"answer": "x", "examples": ["k=2、ℓ=3 时乘积是 25"], "route": "r"}}

        def cmp_(role, n, user):
            if n == 1:
                return {"conflicts": [{"a": "甲", "quote_a": "乘积不超过 24", "b": "乙", "quote_b": "k=2、ℓ=3 时乘积是 25",
                                       "explain": "例子推翻了上界", "severity": "硬伤", "refuted": "甲"}]}
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True}, hooks={"solve": solve, "one": one, "compare": cmp_})
            self.assertEqual(e.flow().run(), "完成")
            self.assertEqual(e.ev("最终状态")[0]["轮数"], 2)  # 第 1 轮被冲突挡住
            ops = [x for x in e.ev("评审意见") if x["层"] == "材料间冲突"]
            self.assertTrue(ops and ops[0]["严重程度"] == "硬伤" and "乘积是 25" in ops[0]["发出的意见"])

    def test_regression_stop_after_refutation_rule_in_prompt(self):
        """回归题④（2.6 GLM 没停）：提示词里有"反驳够用即止"；审查者自己填了答案会被标出越界。"""
        self.assertIn("反驳够用即止", S.SYSTEM_REVIEW_ONE)

        def one(role, n, user, lab):
            return {"own_answer": "我自己算出来是 123"}
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True}, hooks={"one": one})
            e.flow().run()
            self.assertEqual(e.ev("逐份评审")[0]["审查者越界（自己填了答案）"], "我自己算出来是 123")


# ---------------------------------------------------------------- 裸题对照、报告、旧项目

class TestBareAndReport(unittest.TestCase):
    def test_bare_answer_parsing(self):
        self.assertEqual(S.bare_answer("…\n最终答案：$\\boxed{5}$"), "5")
        self.assertEqual(S.bare_answer("所以 \\boxed{k^2}"), "k^2")
        self.assertEqual(S.bare_answer("不知道"), "")

    def test_report_has_form_and_new_stats(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True})
            e.flow().run()
            e.p.log("P1", "裸题对照", ref_type="key", ref_id="Q1|P1", content={"item": "Q1", "role": "P1", "answer": "5"})
            c = report.build(e.p)
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertIn("流程价值的几个计数", md)
            self.assertIn("首轮全错、最终答对", md)
            self.assertIn("冻结的表单", md)
            self.assertIn("裸题对照", md)
            self.assertIn("Q1|P1", json.dumps(c["裸题对照"], ensure_ascii=False))

    def test_attribution_recorded(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_auto_release": True})
            e.flow().run()
            e.p.log("用户", "错误归因", ref_type="key", ref_id="Q1", content={"归因": "表单有误", "note": "漏了量词"})
            report.build(e.p)
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertIn("表单有误", md)

    def test_feed_renders_new_events(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t)
            e.flow().run()
            texts = [m["text"] for m in feed.messages(e.p.root)["messages"]]
            self.assertTrue(any("表单" in x and "第 1 轮" in x for x in texts))
            self.assertTrue(any("确认表单" in x for x in texts))


class TestOldProjectsAndConfig(unittest.TestCase):
    def test_old_project_refused(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as t:
            Project.create(Path(t) / "old", {"project_id": "O", "question": "q", "scope": "s", "task": "final_answer",
                                             "items": [ITEM]}).close()
            (Path(t) / "s.json").write_text(json.dumps({"roles": {"P": {"kind": "生产者", "provider": "mock", "model": "m"},
                                                                  "R": {"kind": "审查者", "provider": "mock", "model": "m"}}}),
                                            encoding="utf-8")
            (Path(t) / "k.env").write_text("", encoding="utf-8")
            r = subprocess.run([sys.executable, "-m", "alreadythere", "run", str(Path(t) / "old"), "--settings", str(Path(t) / "s.json"),
                                "--secrets", str(Path(t) / "k.env")], capture_output=True, cwd=ROOT)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("新建", r.stderr.decode("utf-8") + r.stdout.decode("utf-8"))

    def test_new_sets_flow_version(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as t:
            q = Path(t) / "q.json"
            q.write_text(json.dumps({"project_id": "N", "question": "q", "scope": "s", "task": "final_answer", "items": [ITEM]}),
                         encoding="utf-8")
            subprocess.run([sys.executable, "-m", "alreadythere", "new", str(Path(t) / "n"), "--project", str(q)], cwd=ROOT, check=True,
                           capture_output=True)
            self.assertEqual(json.loads((Path(t) / "n" / "config.json").read_text(encoding="utf-8"))["flow_version"], "2.9")  # 2.9 起新建为 2.9（流程结构与 2.8 相同，多了插话）

    def test_doubao_cap_excludes_thinking(self):
        st = json.loads((ROOT / "examples" / "设置_最强配置.json").read_text(encoding="utf-8"))
        f1 = st["roles"]["F1"]
        self.assertTrue(f1["max_tokens_excludes_thinking"])
        self.assertEqual(f1["max_tokens"], 32768)
        self.assertEqual(st["roles"]["R2"]["extra"]["reasoning_effort"], "max")


if __name__ == "__main__":
    unittest.main()
