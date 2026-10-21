"""2.2 评审流程测试：python -m unittest discover -s tests"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from alreadythere import report, runner
from alreadythere.flow_review import ReviewFlow
from alreadythere.models import MockProvider, ModelHub, TransportError
from alreadythere.store import Project
from alreadythere.tasks import final_answer as T

ITEMS = [{"id": "Q1", "name": "n1", "problem": "PROB-Q1 求值", "gold": "7771777"}]
ANS = {"Q1": "142857", "Q_1": "2", "Q11": "9"}


def _plan(**kw):
    d = {"what": "把答案代回原式检查", "method": "数值代入", "expected": "两边相等", "tolerance": "1e-9"}
    d.update(kw)
    return d


class Env:
    """可编程的模拟模型环境。prod/rev/ver 是函数，返回要覆盖默认值的字典。"""

    def __init__(self, tmp, items=ITEMS, prod=None, rev=None, ver=None, allow_code=False, params=None,
                 producers=("P1", "P2"), reviewers=("R",)):
        cfg = {"project_id": "T", "question": "测试", "scope": "测试", "task": "final_answer", "items": items}
        self.p = Project.create(Path(tmp) / "p", cfg)
        roles = {}
        for r in producers:
            roles[r] = {"kind": "生产者", "provider": "mock", "model": "m" + r, "series": "S" + r}
        for r in reviewers:
            roles[r] = {"kind": "审查者", "provider": "mock", "model": "m" + r, "series": "S" + r}
        pr = {"retry_batch1": [0], "retry_batch2": [], "call_timeout_seconds": 5, "max_concurrency": 3,
              "allow_code_run": allow_code, "code_timeout_seconds": 20, "review_rounds": 5}
        pr.update(params or {})
        self.st = {"roles": roles, "params": pr}
        self.prod, self.rev, self.ver = prod, rev, ver
        self.counts, self.prompts, self.fail = {}, [], None
        self.hub = ModelHub(self.p, self.st, {}, providers={"mock||": MockProvider(self._respond)}, sleep=lambda s: None)

    def flow(self):
        return ReviewFlow(self.p, self.st, self.hub)

    def _respond(self, model, system, user):
        role = model[1:]
        self.prompts.append((role, system, user))
        if self.fail and self.fail(role, system, user):
            raise TransportError("模拟故障")
        iid = next(i["id"] for i in ITEMS_ALL if i["problem"].split()[0] in user)
        key = (role, iid, "P" if "【生产者】" in system else ("V" if "最后一步" in system else "R"))
        self.counts[key] = self.counts.get(key, 0) + 1
        rnd = self.counts[key]
        gold = ANS[iid]
        if key[2] == "P":
            ids = re.findall(r"意见编号：(\S+)", user)
            d = {"answer": gold, "reasoning": "推导", "verify_plan": _plan(), "code": f"print('AUTHOR_{role}')", "limits": ""}
            if ids:
                d["responses"] = [{"opinion": i, "decision": "驳回", "check": "我重新检查了，原做法没有问题。"} for i in ids]
                d["changes"] = []
            if self.prod:
                d.update(self.prod(role, iid, rnd, user, ids) or {})
            return json.dumps(d, ensure_ascii=False)
        labels = re.findall(r"【生产者(.)】", user)
        if key[2] == "R":
            d = {"overall": {l: "通过" for l in labels}, "same_route": False, "answers_equivalent": "不确定", "opinions": [],
                 "implementations": [{"target": l, "check_type": "同路线复现", "check_note": "", "explain": "照描述实现", "code": f"print('IMPL_{l}')"} for l in labels],
                 "fallback_answer": ""}
            if self.rev:
                d.update(self.rev(role, iid, rnd, user, labels) or {})
            return json.dumps(d, ensure_ascii=False)
        d = {"verifications": [{"target": l, "result": "一致", "diagnosis": "无", "reason": "在容差内相符"} for l in labels],
             "opinions": [], "unverified": ["只验证了小情形"]}
        if self.ver:
            d.update(self.ver(role, iid, rnd, user, labels) or {})
        return json.dumps(d, ensure_ascii=False)

    def final(self, iid="Q1"):
        r = self.p.q("SELECT content FROM events WHERE type='最终状态' AND ref_id=?", (iid,))
        return json.loads(r[0]["content"]) if r else None

    def n(self, t):
        return self.p.q("SELECT COUNT(*) c FROM events WHERE type=?", (t,))[0]["c"]


ITEMS_ALL = ITEMS + [{"id": "Q_1", "problem": "ONE 题", "gold": "2"}, {"id": "Q11", "problem": "NINE 题", "gold": "9"}]


def op(target, problem, layer="验证层", severity="硬伤"):
    return {"target": target, "layer": layer, "severity": severity, "basis": "计算错误", "location": "第2步",
            "problem": problem, "internal_note": "对比说明"}


class TestFlow(unittest.TestCase):
    def test_agree_round1_with_code(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, allow_code=True)
            self.assertEqual(e.flow().run(), "完成")
            f = e.final()
            self.assertEqual((f["状态"], f["轮数"], f["共识"], f["一致方式"]), ("闭合", 1, "142857", "程序比对自动一致"))
            # 审查者第一步看不到作者的代码和运行输出（独立实现只依据文字描述）
            rv1 = [u for r, s, u in e.prompts if r == "R" and "【审查者】" in s and "最后一步" not in s]
            self.assertTrue(rv1 and not any("AUTHOR_" in u for u in rv1))
            # 第二步才看到两边的运行结果
            rv2 = [u for r, s, u in e.prompts if "最后一步" in s]
            self.assertTrue(rv2 and "AUTHOR_P1" in rv2[0] and "IMPL_甲" in rv2[0])
            self.assertEqual(e.p.q("SELECT COUNT(*) c FROM runs")[0]["c"], 4)
            self.assertEqual({r["run_type"] for r in e.p.q("SELECT run_type FROM runs")}, {"作者验证", "独立实现"})
            # 审查者从不做题；标准答案不进任何提示词
            self.assertFalse(any("7771777" in u for _r, _s, u in e.prompts))
            self.assertTrue(e.p.verify_chain()[0])
            # 验证概况与未实测项进入结论
            self.assertIn("只验证了小情形", f["未实测项"])
            self.assertEqual(f["验证概况"][0]["独立实现"], "完成")

    def test_code_off_marks_unverified(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, allow_code=False)
            e.flow().run()
            self.assertEqual(e.p.q("SELECT COUNT(*) c FROM runs")[0]["c"], 0)
            f = e.final()
            self.assertEqual(f["状态"], "闭合")
            self.assertTrue(any("未允许运行代码" in u for u in f["未实测项"]))
            self.assertEqual({v["结论"] for v in f["验证概况"]}, {"未能验证"})
            self.assertFalse(any("最后一步" in s for _r, s, _u in e.prompts))  # 没运行就没有第二步

    def test_fix_with_isolation(self):
        def prod(role, iid, rnd, user, ids):
            if role == "P2" and rnd == 1:
                return {"answer": "142858"}
            if role == "P2" and rnd == 2:
                return {"responses": [{"opinion": i, "decision": "采纳", "check": "重算后确实是 142857，原来第二步进位错了"}
                                      for i in ids], "changes": [{"layer": "验证层", "what": "改正进位"}]}

        def rev(role, iid, rnd, user, labels):
            if rnd == 1:
                return {"overall": {"甲": "通过", "乙": "有问题"},
                        "opinions": [op("乙", "你算出 142858，但甲得到142857，乙的第二步进位有误")]}

        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod, rev=rev)
            res = e.flow().run()
            f = e.final()
            self.assertEqual(res, "完成")
            self.assertEqual((f["状态"], f["轮数"]), ("闭合", 2))
            self.assertEqual(f["首轮"], {"P1": "142857", "P2": "142858"})
            self.assertEqual(f["未声明的改答案"], [])
            # P2 修改轮的提示词：有它自己的意见，但不含 P1 的答案和代号
            rev_prompt = [u for r, s, u in e.prompts if r == "P2" and "这是修改轮" in s][0]
            self.assertIn("进位有误", rev_prompt)
            self.assertNotIn("142857", rev_prompt)
            self.assertNotIn("甲", rev_prompt)
            # P1 没有意见：第 2 轮沿用，不调用模型
            self.assertEqual(e.counts.get(("P1", "Q1", "P")), 1)
            self.assertEqual(e.final()["未处理次要意见"], [])
            st = {r["opinion_id"]: r["status"] for r in e.p.q("SELECT opinion_id,status FROM opinions")}
            self.assertEqual(list(st.values()), ["已解决"])
            ev = json.loads(e.p.q("SELECT content FROM events WHERE type='评审意见'")[0]["content"])
            self.assertGreaterEqual(ev["抹去他人信息处数"], 1)
            self.assertIn("对比说明", ev["对比说明（不发给生产者）"])

    def test_stall_goes_to_dispute(self):
        def prod(role, iid, rnd, user, ids):
            if role == "P2":
                return {"answer": "142858"}

        def rev(role, iid, rnd, user, labels):
            return {"overall": {"甲": "通过", "乙": "有问题"}, "opinions": [op("乙", "第二步进位有误")]}

        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod, rev=rev)
            e.flow().run()
            f = e.final()
            self.assertEqual(f["状态"], "有争议")
            self.assertIn("停滞", f["原因"])
            self.assertEqual(f["轮数"], 3)  # 第 2、3 轮连续没有实质改动
            self.assertEqual({x["生产者"] for x in f["争议详情"]}, {"P1", "P2"})
            self.assertIsNone(f["共识"])
            self.assertTrue(e.p.q("SELECT 1 FROM opinions WHERE status='驳回'"))
            md = report.build(e.p)
            self.assertIn("争议详情", (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8"))
            self.assertEqual(md["正确率（基线=各生产者首轮独立作答；最终=评审闭合后的共识）"]["最终"]["对"], 0)

    def test_round_cap(self):
        n = {"i": 0}

        def prod(role, iid, rnd, user, ids):
            if role == "P2":
                n["i"] += 1
                return {"answer": f"14285{n['i']}", "changes": [{"layer": "方法层", "what": "换了做法"}] if ids else []}

        def rev(role, iid, rnd, user, labels):
            return {"overall": {"甲": "通过", "乙": "有问题"}, "opinions": [op("乙", "还是不对")]}

        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod, rev=rev, params={"review_rounds": 2})
            e.flow().run()
            f = e.final()
            self.assertEqual((f["状态"], f["轮数"]), ("有争议", 2))
            self.assertIn("轮数上限", f["原因"])

    def test_default_cap_is_five(self):
        from alreadythere.config import DEFAULT_PARAMS
        self.assertEqual(DEFAULT_PARAMS["review_rounds"], 5)
        self.assertFalse(DEFAULT_PARAMS["allow_code_run"])

    def test_model_judged_equivalence(self):
        items = [{"id": "Q1", "problem": "PROB-Q1 化简", "gold": "2\\sqrt{2}"}]  # 2.3 起 √8 与 2√2 程序可数值验证，改用程序也判不了的写法
        import tests.test_v22 as me  # noqa: F401
        for eq, expect in (("是", "闭合"), ("不确定", "有争议")):
            with tempfile.TemporaryDirectory() as d:
                def prod(role, iid, rnd, user, ids):
                    return {"answer": "\\text{six}" if role == "P2" else "6"}
                e = Env(d, items=items, prod=prod, rev=lambda *a: {"answers_equivalent": eq}, params={"review_rounds": 2})
                global ITEMS_ALL
                keep = ITEMS_ALL
                ITEMS_ALL = items + keep[1:]
                try:
                    e.flow().run()
                finally:
                    ITEMS_ALL = keep
                f = e.final()
                self.assertEqual(f["状态"], expect)
                if eq == "是":
                    self.assertIn("含模型判断", f["一致方式"])

    def test_verification_disagreement_blocks(self):
        def ver(role, iid, rnd, user, labels):
            return {"verifications": [{"target": "甲", "result": "一致", "diagnosis": "无", "reason": "x"},
                                      {"target": "乙", "result": "不一致", "diagnosis": "定义不同", "reason": "统计量定义不同"}],
                    "opinions": [op("乙", "你验证的统计量和题目问的不是同一个量", layer="表示层")]}
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, allow_code=True, ver=ver, params={"review_rounds": 1})
            e.flow().run()
            f = e.final()
            self.assertEqual(f["状态"], "有争议")
            self.assertIn("定义不同", [v for v in f["验证概况"] if v["生产者"] == "P2"][0]["诊断"])

    def test_independence_flags(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, rev=lambda *a: {"same_route": True})
            e.st["roles"]["P2"]["series"] = e.st["roles"]["P1"]["series"]
            e.flow().run()
            ind = e.final()["独立性提示"]
            self.assertEqual(len(ind), 2)
            self.assertTrue(all("独立性未确认" in x for x in ind))

    def test_undeclared_change_flagged(self):
        def prod(role, iid, rnd, user, ids):
            if role == "P2" and rnd == 1:
                return {"answer": "142858"}
            if role == "P2" and rnd == 2:
                return {"answer": "142857", "responses": [{"opinion": i, "decision": "采纳", "check": "重算后确实是 142857"}
                                                         for i in ids], "changes": []}

        def rev(role, iid, rnd, user, labels):
            if rnd == 1:
                return {"overall": {"甲": "通过", "乙": "有问题"}, "opinions": [op("乙", "进位有误")]}
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod, rev=rev)
            e.flow().run()
            self.assertEqual(e.final()["未声明的改答案"], ["P2"])

    def test_soft_opinions_do_not_block_but_are_reported(self):
        def rev(role, iid, rnd, user, labels):
            return {"opinions": [op("甲", "表述可以更简洁", layer="方法层", severity="次要")]}
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, rev=rev)
            e.flow().run()
            f = e.final()
            self.assertEqual(f["状态"], "闭合")
            self.assertTrue(any("更简洁" in x for x in f["未处理次要意见"]))


class TestRobust(unittest.TestCase):
    def test_validation_retries(self):
        n = {"i": 0}

        def rev(role, iid, rnd, user, labels):
            if "格式错误说明" not in user:  # 第一次：评为有问题却没有意见 → 校验不过，要重试
                return {"overall": {"甲": "有问题", "乙": "通过"}, "opinions": []}
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, rev=rev)
            e.flow().run()
            self.assertTrue(any("必须至少写一条" in u for _r, _s, u in e.prompts))
            self.assertEqual(e.final()["状态"], "闭合")

    def test_missing_response_retries(self):
        def prod(role, iid, rnd, user, ids):
            if role == "P2" and rnd == 1:
                return {"answer": "142858"}
            if role == "P2" and rnd == 2 and "格式错误说明" not in user:
                return {"responses": []}
            if role == "P2" and rnd == 3:  # 重试后的合法回应（计数是按调用次数）
                return {"answer": "142857", "responses": [{"opinion": i, "decision": "采纳", "check": "重算后确实是 142857，改正"}
                                                         for i in ids], "changes": [{"layer": "验证层", "what": "x"}]}

        def rev(role, iid, rnd, user, labels):
            if rnd == 1:
                return {"overall": {"甲": "通过", "乙": "有问题"}, "opinions": [op("乙", "进位有误")]}
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod, rev=rev)
            e.flow().run()
            self.assertTrue(any("没有回应" in u for _r, _s, u in e.prompts))
            self.assertEqual(e.final()["状态"], "闭合")

    def test_bare_check_rejected(self):
        v = T.validate_submission(["O1"])
        base = {"answer": "1", "verify_plan": _plan(), "code": "", "limits": "",
                "responses": [{"opinion": "O1", "decision": "采纳", "check": "同意"}], "changes": []}
        from alreadythere.util import FormatError
        with self.assertRaises(FormatError):
            v(dict(base))

    def test_pause_and_resume_without_repeating(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, allow_code=True)
            e.fail = lambda role, system, user: role == "R" and "【审查者】" in system and "最后一步" not in system
            res = e.flow().run()
            self.assertTrue(res.startswith("调用失败暂停"), res)
            self.assertEqual(e.counts[("P1", "Q1", "P")], 1)
            runs_before = e.p.q("SELECT COUNT(*) c FROM runs")[0]["c"]
            e.fail = None
            self.assertEqual(e.flow().run(), "完成")
            self.assertEqual(e.counts[("P1", "Q1", "P")], 1)  # 生产者没有重复调用
            self.assertEqual(e.p.q("SELECT COUNT(*) c FROM runs WHERE run_type='作者验证'")[0]["c"], 2)
            self.assertGreaterEqual(e.p.q("SELECT COUNT(*) c FROM runs")[0]["c"], runs_before)
            self.assertEqual(e.n("提交"), 2)
            self.assertEqual(e.n("最终状态"), 1)
            self.assertTrue(e.p.verify_chain()[0])
            self.assertEqual(e.flow().run(), "已结束")

    def test_like_underscore(self):
        items = [ITEMS_ALL[2], ITEMS_ALL[1]]
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, items=items)
            e.flow().run()
            self.assertEqual(e.final("Q_1")["共识"], "2")
            self.assertEqual(e.final("Q11")["共识"], "9")

    def test_needs_reviewer_and_producer(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                Env(d, reviewers=()).flow()
            with self.assertRaises(ValueError):
                Env(d + "/x", producers=()).flow()

    def test_report_and_gold(self):
        def prod(role, iid, rnd, user, ids):
            return {"answer": "142858"}  # 两个生产者一致但都错
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod)
            e.flow().run()
            rep = report.build(e.p)
            k = "正确率（基线=各生产者首轮独立作答；最终=评审闭合后的共识）"
            self.assertEqual(rep[k]["最终"]["错"], 1)  # 一致不等于正确：标准答案比对仍判错
            self.assertEqual(rep[k]["基线:P1"]["错"], 1)
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            for s in ("程序验证过的", "模型判断的", "未验证的部分"):
                self.assertIn(s, md)


class TestRunner(unittest.TestCase):
    def test_basic(self):
        r = runner.run_code("print(1+1)")
        self.assertEqual((r["status"], r["stdout"].strip()), ("完成", "2"))
        self.assertEqual(runner.run_code("1/0")["status"], "出错")

    def test_timeout_kills(self):
        r = runner.run_code("import time\ntime.sleep(30)", timeout=1)
        self.assertEqual(r["status"], "超时")
        self.assertLess(r["seconds"], 8)

    def test_output_cap(self):
        r = runner.run_code("print('x'*100000)", max_out=500)
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(r["stdout"]), 500)

    def test_secrets_not_inherited(self):
        import os
        os.environ["MY_API_KEY_TEST"] = "sk-secret"
        try:
            r = runner.run_code("import os\nprint(os.environ.get('MY_API_KEY_TEST'), 'sk-secret' in str(os.environ))")
            self.assertEqual(r["stdout"].strip(), "None False")
        finally:
            del os.environ["MY_API_KEY_TEST"]

    def test_runs_in_temp_dir(self):
        r = runner.run_code("import os\nprint(os.getcwd())")
        self.assertIn("gz_run_", r["stdout"])
        self.assertFalse(Path(r["stdout"].strip()).exists())  # 运行完已删除

    def test_probe(self):
        self.assertEqual(set(runner.probe_libs()), set(runner.PROBE_LIBS))


class TestMask(unittest.TestCase):
    def test_mask(self):
        t, n = T.mask_foreign("乙得到 142857，解题者甲也是这样", ["142857"], ["甲", "乙"])
        self.assertNotIn("142857", t)
        self.assertNotIn("甲", t)
        self.assertGreaterEqual(n, 3)
        t, n = T.mask_foreign("答案是 7，你的第二步错了", ["7"], ["甲"])  # 太短的答案不抹，避免误伤
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()


class TestApp(unittest.TestCase):
    def _app(self, d):
        import os
        from alreadythere import app as A
        os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        base = Path(d)
        (base / "设置.json").write_text(json.dumps({
            "roles": {"P1": {"kind": "生产者", "provider": "mock", "model": "m"},
                      "R": {"kind": "审查者", "provider": "mock", "model": "m"}},
            "search": {"engine": "mock"}, "params": {}}), encoding="utf-8")
        items = [{"id": f"Q{i}", "name": f"题{i}", "problem": f"题面{i}", "gold": str(i)} for i in range(1, 6)]
        (base / "q.json").write_text(json.dumps({"project_id": "T", "task": "final_answer", "question": "测试题库",
                                                  "scope": "范围", "items": items}, ensure_ascii=False), encoding="utf-8")
        return A, A.App(base, base)

    def test_questions_list_item_ids(self):
        with tempfile.TemporaryDirectory() as d:
            A, app = self._app(d)
            q = [x for x in app.questions() if x["file"] == "q.json"][0]
            self.assertEqual([i["id"] for i in q["items"]], ["Q1", "Q2", "Q3", "Q4", "Q5"])
            self.assertEqual(q["items"][0]["label"], "题1")

    def test_new_project_with_picked_items(self):
        with tempfile.TemporaryDirectory() as d:
            A, app = self._app(d)
            self.assertFalse(app.new_project("x0", "q.json", [])["ok"])
            self.assertFalse(app.new_project("x1", "q.json", ["Q9"])["ok"])
            self.assertFalse(app.new_project("x2", "q.json", "Q1")["ok"])
            r = app.new_project("sub", "q.json", ["Q3", "Q1", "Q3"])
            self.assertTrue(r["ok"], r)
            cfg = json.loads((Path(d) / "sub" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual([i["id"] for i in cfg["items"]], ["Q3", "Q1"])  # 去重、保持勾选顺序
            self.assertIn("5 题中勾选的 2 题", cfg["scope"])
            self.assertEqual(list(Path(d).glob(".tmp_q_*")), [])  # 临时文件已清理
            self.assertEqual(len(json.loads((Path(d) / "q.json").read_text(encoding="utf-8"))["items"]), 5)  # 原题库不变
            r = app.new_project("all", "q.json")  # 不传 picked ＝ 全部
            self.assertTrue(r["ok"], r)
            self.assertEqual(len(json.loads((Path(d) / "all" / "config.json").read_text(encoding="utf-8"))["items"]), 5)

    def test_params(self):
        with tempfile.TemporaryDirectory() as d:
            A, app = self._app(d)
            v = app.params_view()
            self.assertEqual(v["values"]["review_rounds"], 5)
            self.assertFalse(v["values"]["allow_code_run"])
            self.assertTrue(app.set_params({"review_rounds": 3, "allow_code_run": True, "code_timeout_seconds": 30}, None, {})["ok"])
            st = json.loads((Path(d) / "设置.json").read_text(encoding="utf-8"))
            self.assertEqual((st["params"]["review_rounds"], st["params"]["allow_code_run"]), (3, True))
            self.assertFalse(app.set_params({"review_rounds": 0}, None, {})["ok"])
            self.assertFalse(app.set_params({"review_rounds": 21}, None, {})["ok"])
            self.assertFalse(app.set_params({"code_timeout_seconds": 1}, None, {})["ok"])


class TestFeed(unittest.TestCase):
    def test_feed_renders_every_event(self):
        from alreadythere import feed
        def prod(role, iid, rnd, user, ids):
            if role == "P2" and rnd == 1:
                return {"answer": "142858"}
            if role == "P2" and rnd == 2:
                return {"responses": [{"opinion": i, "decision": "采纳", "check": "重算后确实是 142857，原来进位错了"} for i in ids],
                        "changes": [{"layer": "验证层", "what": "改正进位"}]}

        def rev(role, iid, rnd, user, labels):
            if rnd == 1:
                return {"overall": {"甲": "通过", "乙": "有问题"}, "opinions": [op("乙", "第二步进位有误")]}
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod, rev=rev, allow_code=True)
            e.flow().run()
            msgs = feed.messages(e.p.root)["messages"]
            text = "\n".join(m["text"] for m in msgs)
            for s in ("第 1 轮，独立作答", "修改后", "意见 Q1.r1.R.1", "采纳", "第 2 轮小结", "闭合", "独立验证", "运行 P1 写的作者验证代码",
                      "本次允许运行模型写的代码", "没有收到针对我的意见"):
                self.assertIn(s, text, s)
            self.assertNotIn("None", text)


class TestMemory(unittest.TestCase):
    """2.2.1：生产者修改轮能看到自己此前的记录（含回应、验证过程）；窗口大小读记忆设置；永远看不到别人。"""

    def _scenario(self, d, memory=None, allow_code=False):
        def prod(role, iid, rnd, user, ids):
            if role == "P2" and rnd <= 2:
                return {"answer": f"14285{rnd}", "responses": [{"opinion": i, "decision": "驳回", "check": f"第{rnd}次我重新检查了原做法没有问题"} for i in ids],
                        "reasoning": f"推导R{rnd}", "limits": "只验证了小情形"}
            if role == "P2" and rnd == 3:
                return {"answer": "142857", "responses": [{"opinion": i, "decision": "采纳", "check": "重算后确实是 142857，改正"} for i in ids],
                        "changes": [{"layer": "验证层", "what": "改正"}]}

        def rev(role, iid, rnd, user, labels):
            if rnd <= 2:
                return {"overall": {"甲": "通过", "乙": "有问题"}, "opinions": [op("乙", f"第{rnd}轮意见：进位有误")]}

        def ver(role, iid, rnd, user, labels):
            return {"verifications": [{"target": "甲", "result": "一致", "diagnosis": "无", "reason": "ok"},
                                      {"target": "乙", "result": "部分验证", "diagnosis": "无", "reason": f"验证理由R{rnd}"}]}
        e = Env(d, prod=prod, rev=rev, ver=ver, allow_code=allow_code, params={"review_rounds": 5})
        if memory:
            e.st["roles"]["P2"]["memory"] = memory
        e.flow().run()
        return e

    def _p2_prompts(self, e):
        return [u for r, s, u in e.prompts if r == "P2" and "这是修改轮" in s]

    def test_default_keeps_all_rounds_with_responses_and_verification(self):
        with tempfile.TemporaryDirectory() as d:
            e = self._scenario(d, allow_code=True)
            self.assertEqual(e.final()["状态"], "闭合")
            ps = self._p2_prompts(e)
            self.assertEqual(len(ps), 2)
            p3 = ps[1]  # 第 3 轮
            for s in ("第 1 轮", "第 2 轮", "推导R1", "推导R2", "第2次我重新检查了原做法没有问题", "验证理由R1", "验证理由R2",
                      "IMPL_乙", "它的运行结果", "只验证了小情形"):
                self.assertIn(s, p3, s)
            # 最后一轮的意见只列一次
            self.assertEqual(p3.count("第2轮意见：进位有误"), 1)
            # 隔离：看不到别人的答案、代码、代号
            self.assertNotIn("142857", p3.split("现在需要你逐条回应")[0])
            self.assertNotIn("IMPL_甲", p3)
            self.assertNotIn("AUTHOR_P1", p3)
            self.assertNotIn("甲", p3)

    def test_fixed_window_one_round(self):
        with tempfile.TemporaryDirectory() as d:
            e = self._scenario(d, memory={"mode": "固定", "see": ["self"], "rounds": 1})
            p3 = self._p2_prompts(e)[1]
            self.assertIn("第 2 轮", p3)
            self.assertNotIn("第 1 轮", p3)
            self.assertNotIn("推导R1", p3)

    def test_fresh_equals_previous_round_only_and_global_means_all_own(self):
        with tempfile.TemporaryDirectory() as d:
            e = self._scenario(d, memory={"mode": "全新"})
            self.assertNotIn("推导R1", self._p2_prompts(e)[1])
        with tempfile.TemporaryDirectory() as d:
            e = self._scenario(d, memory={"mode": "全局"})
            p3 = self._p2_prompts(e)[1]
            self.assertIn("推导R1", p3)
            self.assertNotIn("甲", p3)  # 全局在数学题里也不会看到别的生产者

    def test_no_reasoning_length_cap_in_prompt(self):
        self.assertNotIn("400", T.SYSTEM_PRODUCER)
        self.assertIn("不设字数上限", T.SYSTEM_PRODUCER)
