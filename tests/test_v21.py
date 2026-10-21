"""2.1 新增测试：python -m unittest discover -s tests"""

import _winfix  # noqa: F401  Windows：测试结束时清理临时文件夹（见 _winfix.py）
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from alreadythere import app as A


class TestVersionKey(unittest.TestCase):
    def k(self, name):
        return A._version_key(Path(name))

    def test_three_segments(self):
        self.assertEqual(self.k("观者自在 (AlreadyThere)_v2.0"), (2, 0, 0))
        self.assertEqual(self.k("观者自在 (AlreadyThere)_v2.0.1"), (2, 0, 1))
        self.assertGreater(self.k("观者自在 (AlreadyThere)_v2.0.1"), self.k("观者自在 (AlreadyThere)_v2.0"))
        self.assertGreater(self.k("观者自在 (AlreadyThere)_v2.1"), self.k("观者自在 (AlreadyThere)_v2.0.9"))

    def test_multi_digit_patch(self):
        # 2.0.10 比 2.0.9 新，不能被读成 2.0.1
        self.assertGreater(self.k("x_v2.0.10"), self.k("x_v2.0.9"))
        self.assertEqual(self.k("x_v2.0.10"), (2, 0, 10))

    def test_old_names_and_unknown(self):
        self.assertEqual(self.k("观者自在(AlreadyThere)_v1.9"), (1, 9, 0))
        self.assertEqual(self.k("随便什么"), (0, 0, 0))

    def test_migrate_picks_newer_of_201_and_20(self):
        base = Path(tempfile.mkdtemp())
        try:
            new = base / "观者自在 (AlreadyThere)_v2.1"
            for name, tag in (("观者自在 (AlreadyThere)_v2.0", "旧"), ("观者自在 (AlreadyThere)_v2.0.1", "新")):
                d = base / name
                (d / "alreadythere").mkdir(parents=True)
                (d / "alreadythere" / "__init__.py").write_text("", encoding="utf-8")
                (d / A.SETTINGS).write_text(json.dumps({"tag": tag}), encoding="utf-8")
            (new / "alreadythere").mkdir(parents=True)
            (new / "alreadythere" / "__init__.py").write_text("", encoding="utf-8")
            data = base / "数据"
            A.migrate(new, data)
            self.assertEqual(json.loads((data / A.SETTINGS).read_text(encoding="utf-8"))["tag"], "新")
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


# ---------------- 最终答案题包 ----------------
import json as _json
import re as _re
import tempfile as _tmp
import unittest as _ut
from pathlib import Path as _Path

from alreadythere import report as _report
from alreadythere.fields.answer import compare, normalize
from alreadythere.models import MockProvider, ModelHub
from alreadythere.store import Project


class TestAnswerField(_ut.TestCase):
    def test_normalize_and_compare(self):
        self.assertEqual(compare("\\boxed{12}", "12"), "自动一致")
        self.assertEqual(compare("1,000", "1000"), "待人工判断")  # 可能是两个数的列表，交给人
        self.assertEqual(compare("\\dfrac{1}{2}", "0.5"), "自动一致")
        self.assertEqual(compare("x=3", "3"), "自动一致")
        self.assertEqual(compare("7", "8"), "不一致")
        self.assertEqual(compare("2\\sqrt{2}", "\\sqrt{8}"), "自动一致")  # 2.3：数值验证
        self.assertEqual(compare("", "3"), "待人工判断")
        self.assertEqual(normalize("１２。"), "12")


from alreadythere import admission as _adm


class TestAdmission(_ut.TestCase):
    def test_import_and_check(self):
        csv_text = "problem,answer,id\n1+1=?,2,a\n求 x,x=\\frac12,b\n1+1=?,2,c\n证明 1=1,,d\n"
        rows = _adm.read_table(csv_text, "x.csv")
        m = _adm.guess_mapping(_adm.columns_of(rows))
        self.assertEqual((m["problem"], m["gold"], m["id"]), ("problem", "answer", "id"))
        items = _adm.to_items(rows, m)
        codes = {(c["id"], c["code"]) for c in _adm.self_check(items)}
        self.assertIn(("Qc", "dup"), codes)
        self.assertIn(("Qd", "no_gold"), codes)
        self.assertIn(("Qd", "proof"), codes)
        with self.assertRaises(ValueError):
            _adm.build_config("T1", "q", "s", items)
        cfg = _adm.build_config("T1", "q", "s", items, acknowledged=["Qc", "Qd"])
        self.assertEqual(cfg["task"], "final_answer")

    def test_formats(self):
        self.assertEqual(len(_adm.read_table('{"data":[{"a":1}]}', "x.json")), 1)
        self.assertEqual(len(_adm.read_table('{"a":1}\n{"a":2}\n', "x.jsonl")), 2)
        with self.assertRaises(ValueError):
            _adm.read_table("", "x.parquet")


class TestReviewFixes(_ut.TestCase):
    """外部审阅（子代理）指出并经复现的问题。"""

    def test_no_false_agreement(self):
        for a, b in [("A", "a"), ("5M", "5m"), ("2\\pi", "2\\Pi"), ("30°", "30"), ("x=3", "y=3"), ("1,234", "1234")]:
            self.assertNotEqual(compare(a, b), "自动一致", (a, b))
        self.assertEqual(compare("x=3", "3"), "自动一致")

    def test_ids_validated(self):
        mk = lambda ids: [{"id": i, "problem": "p" + i, "gold": "1"} for i in ids]
        for bad in (["Q1", "Q1"], ["a|b"], [""]):
            with self.assertRaises(ValueError):
                _adm.build_config("T1", "q", "s", mk(bad))
        rows = [{"id": "Q002", "p": "x", "a": "1"}, {"p": "y", "a": "2"}, {"id": "Q003", "p": "z", "a": "3"}]
        ids = [i["id"] for i in _adm.to_items(rows, {"problem": "p", "gold": "a", "id": "id"})]
        self.assertEqual(len(set(ids)), 3)


class TestV212(_ut.TestCase):
    def _app(self, d):
        from alreadythere import app as A
        base = _Path(d)
        (base / "设置.json").write_text(_json.dumps({
            "roles": {"P1": {"kind": "生产者", "provider": "openai_compat", "base_url": "https://api.deepseek.com",
                             "model": "m", "extra": {"thinking": {"type": "disabled"}}},
                      "P2": {"kind": "生产者", "provider": "openai_compat", "base_url": "https://api.moonshot.cn/v1",
                             "model": "k", "extra": {"thinking": {"type": "disabled"}}}},
            "search": {"engine": "mock"}, "params": {}}), encoding="utf-8")
        return A, A.App(base, base)

    def test_thinking_switch(self):
        with _tmp.TemporaryDirectory() as d:
            A, app = self._app(d)
            r = app.set_params({}, None, {}, {"P1": {"on": True, "effort": "max"}, "P2": {"on": True, "effort": "max"}})
            self.assertTrue(r["ok"], r)
            st = _json.loads((_Path(d) / "设置.json").read_text(encoding="utf-8"))
            self.assertEqual(st["roles"]["P1"]["extra"], {"thinking": {"type": "enabled"}, "reasoning_effort": "max"})
            self.assertEqual(st["roles"]["P2"]["extra"], {"thinking": {"type": "enabled"}, "reasoning_effort": "max"})  # 2.3：Kimi 也支持强度
            v = app.params_view()
            self.assertTrue(v["roles"][0]["thinking"] and v["roles"][0]["effort_ok"] and v["roles"][1]["effort_ok"])
            self.assertFalse(app.set_params({}, None, {}, {"P1": {"on": True, "effort": "ultra"}})["ok"])
            app.set_params({}, None, {}, {"P1": {"on": False}})
            st = _json.loads((_Path(d) / "设置.json").read_text(encoding="utf-8"))
            self.assertEqual(st["roles"]["P1"]["extra"], {"thinking": {"type": "disabled"}})

    def test_k3_thinking_fixed(self):
        with _tmp.TemporaryDirectory() as d:
            A, app = self._app(d)
            p = _Path(d) / "设置.json"
            st = _json.loads(p.read_text(encoding="utf-8"))
            st["roles"]["P2"]["model"] = "kimi-k3"
            st["roles"]["P2"]["extra"] = {"thinking": {"type": "disabled"}}
            p.write_text(_json.dumps(st), encoding="utf-8")
            self.assertTrue(app.params_view()["roles"][1]["thinking_fixed"])
            self.assertTrue(app.set_params({}, None, {}, {"P2": {"on": False, "effort": "max"}})["ok"])
            st = _json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(st["roles"]["P2"]["extra"], {"reasoning_effort": "max"})  # 不传 thinking，强度照写

    def test_stop(self):
        import subprocess, sys, time
        with _tmp.TemporaryDirectory() as d:
            A, app = self._app(d)
            Project.create(_Path(d) / "pj", {"project_id": "T", "question": "q", "scope": "s", "items": []}).close()
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            (_Path(d) / "pj" / A.LOCK).write_text(f"{proc.pid}\n", encoding="utf-8")
            self.assertEqual(app.stop("nope")["ok"], False)
            self.assertTrue(app.stop("pj")["ok"])
            proc.wait(timeout=5)
            self.assertFalse((_Path(d) / "pj" / A.LOCK).exists())
            pj = Project(_Path(d) / "pj").open()
            self.assertTrue(pj.q("SELECT 1 FROM events WHERE type='用户停止运行'"))
            self.assertTrue(pj.verify_chain()[0])
            self.assertFalse(app.stop("pj")["ok"])  # 已经没在运行
