"""2.8：表单阶段改为开关、默认关（用户 2026-10-21 定）。关的时候直接用原题和口径做题、审题，第一轮就是基线。"""

import _winfix  # noqa: F401  Windows：测试结束时清理临时文件夹（见 _winfix.py）
import json
import tempfile
import unittest

from alreadythere import feed, report
from alreadythere.app import App
from alreadythere.config import DEFAULT_PARAMS
from alreadythere.tasks import stage27 as S

from test_v27 import ITEM, Env27, run_to_end


def env(t, **kw):
    kw.setdefault("flow_version", "2.8")
    return Env27(t, **kw)


class TestFormSwitch(unittest.TestCase):
    def test_default_is_off(self):
        self.assertIs(DEFAULT_PARAMS["form_stage"], False)

    def test_off_solves_on_original_problem(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            self.assertEqual(e.flow().run(), "完成")
            self.assertFalse(e.ev("表单提交"))
            self.assertFalse(e.ev("表单候选"))
            self.assertEqual(e.ev("表单阶段设置")[0]["开启"], False)
            solve = [u for r, s, u in e.prompts if "【生产者】" in s and "不做题" not in s]
            self.assertTrue(solve)
            self.assertTrue(all("What is the exact value" in u for u in solve))  # 生产者看原题
            self.assertFalse(any("题目表单（已冻结" in u for u in solve))
            ones = e.calls(S.SYSTEM_REVIEW_ONE_RAW)
            self.assertTrue(ones)
            self.assertFalse(e.calls(S.SYSTEM_REVIEW_ONE))
            self.assertTrue(all("【原题】" in u and "What is the exact value" in u for _, u in ones))  # 审查者看原题
            ffs = e.calls(S.SYSTEM_FAULTFINDER)
            self.assertTrue(ffs and all("What is the exact value" in u for _, u in ffs))  # 找茬者看原题
            self.assertEqual(e.ev("最终状态")[0]["状态"], "闭合")
            self.assertEqual(e.ev("逐份评审")[0]["逐条结论"][0]["req"], "原题")

    def test_on_behaves_like_27(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t, params={"form_stage": True})
            self.assertTrue(e.flow().run().startswith("等待确认表单"))
            self.assertTrue(e.ev("表单阶段设置")[0]["开启"])
            self.assertEqual(run_to_end(e), "完成")
            self.assertTrue(e.calls(S.SYSTEM_REVIEW_ONE))
            self.assertFalse(e.calls(S.SYSTEM_REVIEW_ONE_RAW))

    def test_27_project_always_uses_form(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env27(t, params={"form_stage": False})  # 2.7 建的项目：不受开关影响
            self.assertTrue(e.flow().run().startswith("等待确认表单"))
            self.assertFalse(e.ev("表单阶段设置"))

    def test_setting_fixed_after_first_run(self):
        """第一次运行定下后，中途改参数不影响本项目。"""
        with tempfile.TemporaryDirectory() as t:
            e = env(t, params={"review_rounds": 1})
            e.flow().run()
            e.st["params"]["form_stage"] = True
            f = e.flow()
            self.assertFalse(f.use_form)
            self.assertEqual(len(e.ev("表单阶段设置")), 1)

    def test_hard_flaw_on_original_blocks_closure(self):
        fired = []

        def one(role, n, user, lab):  # 只对甲的第一次审查给硬伤（并行时调用顺序不固定，不能用 n 判断）
            if lab == "甲" and not fired:
                fired.append(1)
                return {"overall": "有问题", "requirements": [{"req": "原题", "verdict": "不满足"}],
                        "opinions": [{"severity": "硬伤", "basis": "与口径冲突", "requirement": "原题",
                                      "problem": "原题说「all pairs」，答卷固定了底集"}]}
        with tempfile.TemporaryDirectory() as t:
            e = env(t, hooks={"one": one})
            self.assertEqual(e.flow().run(), "完成")
            f = e.ev("最终状态")[0]
            self.assertGreater(f["轮数"], 1)  # 第 1 轮被挡住

    def test_raw_validator_only_accepts_original(self):
        v = S.validate_review_one("甲", [S.RAW_REQ], [], "答卷全文", "原题")
        bad = {"overall": "有问题", "requirements": [{"req": "原题", "verdict": "不满足"}],
               "opinions": [{"severity": "硬伤", "basis": "反例", "requirement": "3", "problem": "x"}]}
        with self.assertRaises(Exception) as cm:
            v(bad)
        self.assertIn("只能写 原题", str(cm.exception))

    def test_raw_prompt_follows_prompt_principle(self):
        """提示词原则：不用角色比喻、评价词。"""
        for w in ("像一位", "老师", "你的价值", "认真", "不要为了凑数"):
            self.assertNotIn(w, S.SYSTEM_REVIEW_ONE_RAW)

    def test_report_without_form_section(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            e.flow().run()
            report.build(e.p)
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertNotIn("冻结的表单", md)
            self.assertIn("流程价值的几个计数", md)

    def test_feed_and_app(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            e.flow().run()
            texts = [m["text"] for m in feed.messages(e.p.root)["messages"]]
            self.assertTrue(any("不拆表单" in x for x in texts))
            a = App.__new__(App)
            a.base = e.p.root.parent
            r = a.forms(e.p.root.name)
            self.assertTrue(r["math"])
            self.assertFalse(r["form"])


if __name__ == "__main__":
    unittest.main()
