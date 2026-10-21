"""2.4：答案三态（得出/部分得出/未得出）、宽松格式校验、终止也出结论、代码运行环境记录。"""

import _winfix  # noqa: F401  Windows：测试结束时清理临时文件夹（见 _winfix.py）
import json
import tempfile
import unittest
from pathlib import Path

from alreadythere import report, runner
from alreadythere.models import CallFailedPause
from alreadythere.tasks import final_answer as T
from alreadythere.util import FormatError
from tests.test_v22 import Env, ITEMS_ALL, op

UNK = {"tried": "试了代入小情形与递推，都没有收敛", "stuck": "第三步的求和找不到闭式", "missing": "一个关于该和式的恒等式"}


def abstain(role, iid, rnd, user, ids):
    return {"status": "未得出", "answer": "", "unknown": UNK, "code": "", "verify_plan": {}}


def good_sub(**kw):
    d = {"status": "得出", "answer": "5", "reasoning": "r",
         "verify_plan": {"what": "w", "method": "m", "expected": "e", "tolerance": "无"}, "code": "", "limits": ""}
    d.update(kw)
    return d


class TestValidate(unittest.TestCase):
    def v(self, **kw):
        o = good_sub(**kw)
        T.validate_submission()(o)
        return o

    def test_abstain_needs_reason(self):
        with self.assertRaises(FormatError):
            self.v(status="未得出", answer="")
        with self.assertRaises(FormatError):
            self.v(status="未得出", answer="", unknown={"tried": "x", "stuck": "y"})
        o = self.v(status="未得出", answer="乱写的", unknown=UNK, verify_plan={})
        self.assertEqual(o["answer"], "")  # 未得出时答案一栏一律清空，不让编出来的东西混进去
        self.assertEqual(o["status"], "未得出")

    def test_partial(self):
        with self.assertRaises(FormatError):
            self.v(status="部分得出", answer="", segments=[])
        o = self.v(status="部分得出", answer="", unknown=UNK,
                   segments=[{"condition": "p<2", "value": "3-p", "state": "确定"},
                             {"condition": "p\\ge 2", "value": "?", "state": "未确定"}])
        self.assertEqual([s["state"] for s in o["segments"]], ["确定", "未确定"])
        self.assertIn("p<2", T.segments_text(o))

    def test_missing_status_inferred(self):
        o = good_sub()
        del o["status"]
        T.validate_submission()(o)
        self.assertEqual(o["status"], "得出")
        with self.assertRaises(FormatError):
            T.validate_submission()({"answer": "", "reasoning": "x"})

    def test_verify_plan_missing_fields_only_warn(self):
        o = self.v(verify_plan={"what": "w"})
        self.assertEqual(o["verify_plan"]["method"], "（未写明）")
        self.assertTrue(any("verify_plan" in w for w in o["warnings"]))

    def test_opinion_fields_lenient(self):
        v = T.validate_review(["甲", "乙"])
        o = {"overall": {"甲": "通过", "乙": "有问题"},
             "opinions": [{"target": "乙", "severity": "严重", "basis": "计算错误", "problem": "代码调用了不存在的函数", "layer": "实现层"}]}
        v(o)
        c = o["opinions"][0]
        self.assertEqual((c["layer"], c["severity"], c["location"]), ("实现层", "硬伤", "（未指明）"))  # 写得不标准按硬伤，不打回
        o = {"overall": {"甲": "通过", "乙": "通过"}, "opinions": [{"target": "甲", "severity": "次要", "problem": "小问题"}]}
        v(o)
        self.assertEqual(o["opinions"][0]["layer"], "")
        # 真的无法使用的才打回，而且错误说明是具体的
        with self.assertRaises(FormatError) as cm:
            v({"overall": {"甲": "通过", "乙": "通过"}, "opinions": [{"target": "P1", "severity": "硬伤", "problem": "x"}]})
        self.assertIn("P1", str(cm.exception))
        self.assertIn("甲、乙", str(cm.exception))
        with self.assertRaises(FormatError) as cm:
            v({"overall": {"甲": "通过", "乙": "通过"}, "opinions": [{"target": "甲", "severity": "硬伤"}]})
        self.assertIn("problem", str(cm.exception))

    def test_changes_layer_optional(self):
        o = good_sub(responses=[{"opinion": "A", "decision": "采纳", "check": "重算后确认确实如此"}],
                     changes=[{"what": "改了第二步"}, {"layer": "实现层", "what": "换了函数"}])
        T.validate_submission(["A"])(o)
        self.assertEqual([c["layer"] for c in o["changes"]], ["", "实现层"])

    def test_prefilled_form(self):
        sch = T.schema_review(["甲", "乙"])
        self.assertIn('"甲": "通过 / 有问题 / 不确定", "乙"', sch)
        self.assertIn("必填 target、severity、problem", sch)
        sv = T.schema_verify(["甲", "乙"], ["乙"])
        self.assertIn('"target": "乙", "result"', sv)
        self.assertNotIn('"target": "甲", "result"', sv)


class TestThreeState(unittest.TestCase):
    def test_all_abstain_closes_as_not_obtained(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=abstain)
            self.assertEqual(e.flow().run(), "完成")
            f = e.final()
            self.assertEqual((f["状态"], f["轮数"]), ("未得出", 1))
            self.assertIsNone(f["共识"])
            self.assertEqual(set(f["各方最终状态"].values()), {"未得出"})
            self.assertIn("卡在", f["未得出或部分得出者"]["甲"]["未解决"])
            # 标准答案比对记成"未得出"，不算错
            g = {r["ref_id"]: json.loads(r["content"]) for r in e.p.q("SELECT ref_id,content FROM events WHERE type='对照标准答案'")}
            self.assertEqual({v["result"] for v in g.values()}, {"未得出"})
            # 审查者提示词里看到了"未得出"的状态和理由，也被告知要检查理由是否成立
            rv = [(s, u) for r, s, u in e.prompts if r == "R"][0]
            self.assertIn("状态：未得出", rv[1])
            self.assertIn("放弃得太早", rv[0])
            self.assertTrue(e.p.verify_chain()[0])

    def test_one_known_is_not_closed(self):
        def prod(role, iid, rnd, user, ids):
            return abstain(role, iid, rnd, user, ids) if role == "P2" else None
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod)
            e.flow().run()
            f = e.final()
            self.assertEqual(f["状态"], "仅一方得出")
            self.assertEqual(f["共识"], "142857")
            self.assertEqual(f["轮数"], 1)  # 没有任何意见可发，不会白白迭代到上限
            self.assertIn("没有可发给生产者的意见", f["原因"])
            g = {r["ref_id"]: json.loads(r["content"]) for r in e.p.q("SELECT ref_id,content FROM events WHERE type='对照标准答案'")}
            self.assertEqual(g["Q1|基线:P2"]["result"], "未得出")
            self.assertIn("仅一方得出", g["Q1|最终"]["判定方式"])
            c = report.build(e.p)
            s = c["正确率（基线=各生产者首轮独立作答；最终=评审闭合后的共识）"]["最终"]
            self.assertEqual((s["对"], s["仅一方得出"]), (0, 1))  # 仅一方得出不计为对

    def test_two_agree_one_abstains_closes(self):
        def prod(role, iid, rnd, user, ids):
            return abstain(role, iid, rnd, user, ids) if role == "P3" else None
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod, producers=("P1", "P2", "P3"))
            e.flow().run()
            f = e.final()
            self.assertEqual(f["状态"], "闭合")
            self.assertIn("未完整得出", f["一致方式"])
            self.assertEqual(f["共识"], "142857")

    def test_reviewer_challenges_abstain_then_producer_solves(self):
        def prod(role, iid, rnd, user, ids):
            if role == "P2" and rnd == 1:
                return abstain(role, iid, rnd, user, ids)
            if role == "P2" and rnd == 2:
                return {"status": "得出", "answer": "142857", "unknown": {},
                        "responses": [{"opinion": i, "decision": "采纳", "check": "按意见换了递推思路后算出了结果"} for i in ids],
                        "changes": [{"what": "换了方法并得出答案"}]}

        def rev(role, iid, rnd, user, labels):
            if rnd == 1:
                return {"overall": {"甲": "通过", "乙": "有问题"},
                        "opinions": [{"target": "乙", "severity": "硬伤", "basis": "弃权理由不成立", "problem": "放弃得太早，第三步可以直接递推求和"}]}
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod, rev=rev)
            e.flow().run()
            f = e.final()
            self.assertEqual((f["状态"], f["轮数"]), ("闭合", 2))
            self.assertEqual(f["未声明的改答案"], [])  # 状态从未得出变成得出，已声明改动

    def test_both_partial(self):
        def prod(role, iid, rnd, user, ids):
            return {"status": "部分得出", "answer": "", "unknown": UNK,
                    "segments": [{"condition": "p<2", "value": "3-p", "state": "确定"},
                                 {"condition": "p\\ge 2", "value": "?", "state": "未确定"}]}
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=prod)
            e.flow().run()
            f = e.final()
            self.assertEqual(f["状态"], "部分得出")
            self.assertIn("p<2", f["未得出或部分得出者"]["甲"]["已确定"])

    def test_report_three_states_and_accuracy(self):
        items = [{"id": "Q1", "name": "a", "problem": "PROB-Q1 x", "gold": "142857"},
                 {"id": "Q11", "name": "b", "problem": "NINE 题", "gold": "9"}]

        def prod(role, iid, rnd, user, ids):
            return abstain(role, iid, rnd, user, ids) if iid == "Q11" else None
        import tests.test_v22 as m
        keep = m.ITEMS_ALL
        m.ITEMS_ALL = items + [x for x in keep if x["id"] not in ("Q1", "Q11")]
        try:
            with tempfile.TemporaryDirectory() as d:
                e = Env(d, items=items, prod=prod)
                e.flow().run()
                c = report.build(e.p)
                s = c["正确率（基线=各生产者首轮独立作答；最终=评审闭合后的共识）"]["最终"]
                self.assertEqual((s["对"], s["错"], s["未得出"]), (1, 0, 1))
                self.assertEqual(s["已判定的题里对多少"], "1/1")
                self.assertEqual(s["选择不答的比例"], "1/2")
                md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
                self.assertIn("选择不答的比例", md)
                self.assertIn("未完整得出的生产者", md)
                self.assertEqual(c["项目状态"], "已完成")
                self.assertNotIn("终止原因", c)
        finally:
            m.ITEMS_ALL = keep

    def test_terminated_project_still_reports(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            e.p.log("用户", "用户停止运行", content={"说明": "x"})
            c = report.build(e.p)
            self.assertEqual((c["项目状态"], c["结论"]), ("未完成", "未得出"))
            self.assertIn("用户终止", c["终止原因"])
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertIn("终止原因", md)

    def test_feed_renders_new_states(self):
        from alreadythere import feed
        with tempfile.TemporaryDirectory() as dd:
            e = Env(dd, prod=lambda role, iid, rnd, user, ids: abstain(role, iid, rnd, user, ids) if role == "P2" else None)
            e.flow().run()
            text = "\n".join(m["text"] for m in feed.messages(e.p.root)["messages"])
            self.assertIn("【未得出】", text)
            self.assertIn("仅一方得出", text)
            self.assertIn("卡在", text)
            self.assertNotIn("None", text)


class TestFormatRetry(unittest.TestCase):
    def test_format_retry_limit(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=lambda *a: {"status": "", "answer": ""}, params={"retry_batch1": [0, 0, 0], "retry_batch2": [0, 0, 0],
                                                                         "format_retry_max": 3})
            r = e.flow().run()
            self.assertIn("调用失败暂停", r)
            n = e.p.q("SELECT COUNT(*) c FROM calls WHERE role='P1' AND status='失败'")[0]["c"]
            self.assertEqual(n, 3)  # 默认 7 次，现在格式类失败 3 次就停
            # 重试时的错误说明是具体的
            req = e.p.q("SELECT request FROM calls WHERE call_id LIKE 'p-P1-Q1-r1#2'")[0]["request"]
            self.assertIn("缺少 status", req)


class TestRunnerEnv(unittest.TestCase):
    def test_env_recorded(self):
        r = runner.run_code("import random\nrandom.seed(7)\nprint(random.random())", timeout=20)
        self.assertEqual(r["env"]["declared_seed"], "7")
        self.assertTrue(r["env"]["uses_random"])
        r = runner.run_code("import random\nprint(random.random())", timeout=20)
        self.assertIn("没有声明种子", r["env"]["seed_note"])
        self.assertEqual(runner.run_code("print(1)", timeout=20)["env"]["seed_note"], "代码没有用随机数")

    def test_env_in_event(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, allow_code=True)
            e.flow().run()
            ev = json.loads(e.p.q("SELECT content FROM events WHERE type='代码运行' ORDER BY seq")[0]["content"])
            self.assertIn("python", ev["环境"])


if __name__ == "__main__":
    unittest.main()
