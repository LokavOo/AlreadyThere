"""第一期测试：python -m unittest discover -s tests"""

import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from guanzhe import blocks as B, demo, report
from guanzhe.tasks import fact_compilation as T
from guanzhe.__main__ import load_settings_from_obj
from guanzhe.blocks import locate_quote
from guanzhe.flow import Flow
from guanzhe.models import CallFailedPause, FatalCallError, ModelHub, TransportError
from guanzhe.retrieval import MockSearch, Retriever
from guanzhe.store import Project
from guanzhe.tasks.fact_compilation import normalize_date, zodiac_of
from guanzhe.util import extract_json, find_numbers, written_matches_output


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def demo_project(self, name="p"):
        p = Project.create(self.dir / name, demo.project_config())
        st = load_settings_from_obj(demo.settings())
        return p, st


class TestUtil(unittest.TestCase):
    def test_numbers(self):
        vals = dict(find_numbers("约四成，62%，0.62，一三二八年"))
        self.assertIsNone(vals["四"] if "四" in vals else None)
        self.assertEqual(vals["62%"], Decimal("0.62"))
        self.assertEqual(vals["一三二八"], Decimal(1328))

    def test_written_precision(self):
        self.assertTrue(written_matches_output("0.333", 0.333333))
        self.assertFalse(written_matches_output("0.34", 0.333333))

    def test_extract_json(self):
        self.assertEqual(extract_json('说明```json\n{"a": 1}\n```')["a"], 1)
        self.assertEqual(extract_json('xx {"a": {"b": "}"}} yy')["a"]["b"], "}")

    def test_zodiac(self):
        self.assertEqual(zodiac_of(normalize_date("1924-05-01"))[0], "鼠")
        self.assertIsNone(zodiac_of(normalize_date("1924-02-10"))[0])  # 须核对农历
        self.assertIsNone(zodiac_of(normalize_date("1924"))[0])  # 仅知年份

    def test_locate_quote(self):
        text = "第一段。\n测试甲帝生于1500年10月3日，卒于1560年。\n第三段。"
        ok, off, para, ctx = locate_quote(text, "测试甲帝生于 1500年10月3日")
        self.assertTrue(ok)
        self.assertIn("第一段", ctx)
        self.assertFalse(locate_quote(text, "测试甲帝降生于1500年")[0])


class TestLedger(Tmp):
    def test_tamper_detected(self):
        p, _ = self.demo_project()
        p.log("程序", "测试", content={"x": 1})
        p.log("程序", "测试", content={"x": 2})
        self.assertTrue(p.verify_chain()[0])
        c = sqlite3.connect(p.db_path)
        c.execute("UPDATE events SET content='{\"x\":999}' WHERE seq=2")
        c.commit()
        ok, bad, _ = p.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(bad, "E000002")


class FlakyProvider:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, model, system, user, max_tokens, timeout):
        self.calls.append(user)
        x = self.script.pop(0)
        if isinstance(x, Exception):
            raise x
        return x, 10, 10


class TestRetry(Tmp):
    def hub(self, script):
        p, st = self.demo_project()
        prov = FlakyProvider(script)
        waits = []
        hub = ModelHub(p, st, {}, providers={"mock|": prov}, sleep=waits.append)
        return p, hub, prov, waits

    def test_transport_then_success(self):
        p, hub, prov, _ = self.hub([TransportError("x"), TransportError("y"), '{"ok": 1}'])
        self.assertEqual(hub.call_json("P1", "s", "u", call_key="k1", round_=1)["ok"], 1)
        self.assertEqual(len(prov.calls), 3)
        self.assertEqual(prov.calls[0], prov.calls[2])  # 传输类重试原样重发

    def test_format_retry_appends_note(self):
        p, hub, prov, _ = self.hub(["不是 JSON", '{"ok": 1}'])
        hub.call_json("P1", "s", "u", call_key="k2", round_=1)
        self.assertIn("格式错误说明", prov.calls[1])

    def test_fatal_pauses_immediately(self):
        p, hub, prov, _ = self.hub([FatalCallError("鉴权失败")])
        with self.assertRaises(CallFailedPause):
            hub.call_json("P1", "s", "u", call_key="k3", round_=1)
        self.assertEqual(len(prov.calls), 1)
        self.assertTrue(p.q("SELECT 1 FROM events WHERE type='调用失败暂停'"))

    def test_all_retries_fail_then_pause(self):
        p, hub, prov, _ = self.hub([TransportError("x")] * 7)
        with self.assertRaises(CallFailedPause):
            hub.call_json("P1", "s", "u", call_key="k4", round_=1)
        self.assertEqual(len(prov.calls), 7)  # 首次 + 两批各 3 次


class TestDemoFlow(Tmp):
    def run_demo(self):
        p, st = self.demo_project()
        hub = ModelHub(p, st, {}, providers=demo.providers(), sleep=lambda s: None)
        res = Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES))).run()
        return p, res, report.build(p)

    def test_outcomes(self):
        p, res, c = self.run_demo()
        self.assertEqual(res, "完成")
        byid = {r["条目"]: r for r in c["逐条结果"]}
        self.assertEqual(byid["I01"]["结论"], "查到")
        self.assertEqual(byid["I01"]["独立验证次数"], 2)  # 两方网站不同 1 + 另一可信网站独立支持 1
        self.assertEqual(byid["I02"]["独立验证次数"], 1)  # 两方同一网站不计，只计独立来源
        self.assertEqual(byid["I02"]["结论"], "查到")      # 1.4：同一网站＋另一可信网站独立支持 → 查到
        self.assertIn("同引", byid["I02"]["备注"])
        self.assertEqual(byid["I05"]["结论"], "查到")      # 农历原文由程序换算
        self.assertEqual(byid["I05"]["出生日期"], "1399-03-16")
        self.assertEqual(byid["I05"]["生肖"], "兔")
        self.assertEqual(byid["I03"]["结论"], "有争议")
        self.assertEqual(byid["I04"]["结论"], "资料缺载")
        self.assertTrue(c["账本完整性"]["完整"])

    def test_fabricated_quote_caught(self):
        p, _, _ = self.run_demo()
        b = p.q("SELECT * FROM blocks WHERE block_id='P2-R1-B002'")[0]
        self.assertEqual(b["st_check"], "未查证")
        self.assertIn("原句未在所注检索的存档原文中找到", b["flags"])

    def test_resume_uses_cached_calls(self):
        p, st = self.demo_project()
        router = demo.providers()["mock|"]
        orig = router.map["mock-c"]
        state = {"fail": True}

        def flaky(model, system, user):
            if state["fail"]:
                raise TransportError("审查者掉线")
            return orig(model, system, user)
        router.map["mock-c"] = flaky
        hub = ModelHub(p, st, {}, providers={"mock|": router}, sleep=lambda s: None)
        res = Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES))).run()
        self.assertTrue(res.startswith("调用失败暂停"))
        n_calls_before = p.q("SELECT COUNT(*) c FROM calls WHERE role='P1' AND status='成功'")[0]["c"]
        n_retr_before = p.q("SELECT COUNT(*) c FROM retrievals")[0]["c"]
        state["fail"] = False
        res2 = Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES))).run()
        self.assertEqual(res2, "完成")
        # 第 1 轮生产者的调用没有重复发出；检索也没有重复
        r1 = p.q("SELECT COUNT(*) c FROM calls WHERE role='P1' AND call_id LIKE 'R1-%' AND status='成功'")[0]["c"]
        self.assertEqual(r1, 2)
        self.assertEqual(p.q("SELECT COUNT(*) c FROM retrievals WHERE round=1")[0]["c"], n_retr_before)
        self.assertTrue(p.verify_chain()[0])



class Test14(Tmp):
    """1.4 新增：去排版比对、历表换算、记忆模式与上下文断开、独立核验、规则调整。"""

    def run_demo(self, memory=None):
        p, st = self.demo_project()
        if memory:
            for r in ("P1", "P2"):
                st["roles"][r]["memory"] = memory
        hub = ModelHub(p, st, {}, providers=demo.providers(), sleep=lambda s: None)
        Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES))).run()
        return p

    def test_markdown_quote_matches(self):
        raw = "| 出生 | [1360年](https://x/1360)5月2日（至正二十年四月十七） |\n**朱棣**生于应天"
        ok, *_ = B.locate_quote(raw, "出生 1360年5月2日（至正二十年四月十七）")
        self.assertTrue(ok)
        ok2, *_ = B.locate_quote("朱元璋，生於天曆元年", "朱元璋，生于天历元年")  # 繁简不同也能对上
        self.assertTrue(ok2)
        self.assertFalse(B.locate_quote(raw, "朱棣生于1361年")[0])

    def test_calendar(self):
        from guanzhe import cn_calendar as C
        cases = {"天历元年九月十八": "1328-10-21", "建文元年二月初九日": "1399-03-16",
                 "顺治十一年三月十八": "1654-05-04", "万历三十八年十二月廿四": "1611-02-06",
                 "光绪三十二年正月十四": "1906-02-07"}
        for t, d in cases.items():
            self.assertEqual(C.from_text(t)[1]["date"], d, t)
        self.assertEqual(C.lunar_year_of_jd(C.date_to_jd(1611, 2, 6)), "庚戌")  # 公历 1611 年初仍是农历庚戌（狗）年

    def test_aliases(self):
        a = T.aliases({"name": "清圣祖玄烨（康熙）"})
        self.assertIn("玄烨", a)
        self.assertIn("康熙", a)

    def test_isolated_opinions_hide_other_values(self):
        p = self.run_demo()
        for o in p.q("SELECT * FROM opinions WHERE kind='分歧'"):
            other = "1541" if o["target_author"] == "P1" else "1540"
            self.assertIn(other, o["content"])
            self.assertNotIn(other, o["content_isolated"])
        # 生产者（固定模式）收到的提示词里没有对方的数值
        for c in p.q("SELECT call_id, request FROM calls WHERE call_id LIKE 'R2-P1-%'"):
            u = json.loads(c["request"])["user"]
            ops = u.split("意见")[1] if "意见" in u else ""
            self.assertNotIn("1541", ops.split("输出格式")[0])
            self.assertNotIn("其他生产者", u)

    def test_global_mode_sees_others(self):
        p = self.run_demo({"mode": "全局"})
        u = "".join(json.loads(c["request"])["user"] for c in p.q("SELECT request FROM calls WHERE call_id LIKE 'R2-P1-write%'"))
        self.assertIn("其他生产者上一轮的条目块", u)

    def test_fresh_mode_no_history(self):
        p = self.run_demo({"mode": "全新"})
        u = "".join(json.loads(c["request"])["user"] for c in p.q("SELECT request FROM calls WHERE call_id LIKE 'R2-P%'"))
        self.assertNotIn("你之前提交的条目块", u)
        self.assertNotIn("其他生产者", u)

    def test_independent_search_has_no_numbers(self):
        p = self.run_demo()
        rows = p.q("SELECT query FROM retrievals WHERE requester='R1'")
        self.assertTrue(rows)
        for r in rows:
            self.assertFalse(re.search(r"\d", r["query"]))

    def test_cannot_cite_others_retrieval(self):
        p, st = self.demo_project()
        hub = ModelHub(p, st, {}, providers=demo.providers(), sleep=lambda s: None)
        f = Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES)))
        rid = f.ret.search("P2", 1, "测试甲帝 出生", 1, item_id="I01")[0]["retrieval_id"]
        flags, st_check = f.check_block("P1", "I01", {"状态": "查到", "出处": rid, "原句": "测试甲帝生于1500年10月3日",
                                                       "出生日期": "1500-10-03"})
        self.assertEqual(st_check, "未查证")
        self.assertIn("不是你自己检索到的", flags[0])

    def test_lunar_mismatch_caught(self):
        p, st = self.demo_project()
        hub = ModelHub(p, st, {}, providers=demo.providers(), sleep=lambda s: None)
        f = Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES)))
        rid = f.ret.search("P1", 1, "测试戊帝 出生", 1, item_id="I05")[0]["retrieval_id"]
        base = {"状态": "查到", "出处": rid, "原句": "出生 建文元年二月初九日", "农历原文": "建文元年二月初九日"}
        _, s1 = f.check_block("P1", "I05", dict(base, 出生日期="1399-02-09"))  # 把农历当公历写
        self.assertEqual(s1, "换算不符")
        fx = dict(base, 出生日期="1399")
        _, s2 = f.check_block("P1", "I05", fx)  # 只写年份：按农历补全
        self.assertEqual((s2, fx["出生日期"]), ("待审查", "1399-03-16"))
        fy = dict(base, 出生日期="1399-03-24")  # 格里历外推写法：改为儒略历
        _, s3 = f.check_block("P1", "I05", fy)
        self.assertEqual((s3, fy["出生日期"]), ("待审查", "1399-03-16"))

    def test_calendar_convention_not_a_dispute(self):
        # 1328-10-21（儒略历）与 1328-10-29（格里历外推）是同一天
        self.assertEqual(Flow.agree([(1328, 10, 21), (1328, 10, 29)]), (1328, 10, 21))
        from guanzhe import cn_calendar as C
        js = [j for _, j in C.dates_in("朱元璋生于天历元年九月十八日")][0]
        self.assertIn(C.date_to_jd(1328, 10, 21), js)

    def test_year_compatible(self):
        self.assertEqual(Flow.agree([(1399, None, None), (1399, 3, 16)]), (1399, 3, 16))
        self.assertIsNone(Flow.agree([(1398, None, None), (1399, 3, 16)]))
        self.assertIsNone(Flow.agree([(1399, 3, 15), (1399, 3, 16)]))

    def test_old_project_migrates(self):
        import sqlite3
        d = self.dir / "old"
        p = Project.create(d, {"project_id": "O", "question": "q", "scope": "s", "items": []})
        p.close()
        c = sqlite3.connect(d / "project.db")
        c.execute("ALTER TABLE items DROP COLUMN ganzhi")
        c.commit()
        c.close()
        p2 = Project(d).open()
        self.assertIn("ganzhi", {r[1] for r in p2.conn.execute("PRAGMA table_info(items)")})


class TestExtraParams(unittest.TestCase):
    def test_extra_and_max_tokens_reach_payload(self):
        import guanzhe.models as M
        seen = {}
        def fake_post(url, headers, payload, timeout):
            seen.update(payload)
            return {"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}], "usage": {}}
        orig = M._http_post
        M._http_post = fake_post
        try:
            d = Path(tempfile.mkdtemp())
            proj = Project.create(d / "x", {"project_id": "X", "question": "", "scope": "", "items": []})
            st = load_settings_from_obj({"roles": {"P1": {"kind": "生产者", "provider": "openai_compat",
                  "base_url": "https://example.invalid", "key_name": "K", "model": "m", "series": "S",
                  "max_tokens": 16000, "extra": {"thinking": {"type": "disabled"}, "model": "bad"}}},
                  "search": {"engine": "mock"}})
            hub = ModelHub(proj, st, {"K": "k"})
            hub.call_json("P1", "s", "u", call_key="t", round_=0, max_tokens=300)
            self.assertEqual(seen["thinking"], {"type": "disabled"})
            self.assertEqual(seen["max_tokens"], 16000)
            self.assertEqual(seen["model"], "m")
            row = proj.q("SELECT params FROM calls")[0]["params"]
            self.assertIn("disabled", row)
        finally:
            M._http_post = orig
            shutil.rmtree(d, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()


class TestHardening(Tmp):
    def test_bom_secrets(self):
        from guanzhe.config import load_secrets
        f = self.dir / "k.env"
        f.write_bytes("﻿ANTHROPIC_API_KEY=abc\n".encode("utf-8"))
        self.assertEqual(load_secrets(f).get("ANTHROPIC_API_KEY"), "abc")

    def test_search_auth_failure_pauses(self):
        import urllib.error
        p, st = self.demo_project()

        class Bad:
            def search(self, q, n):
                raise urllib.error.HTTPError("u", 401, "no", {}, None)
        with self.assertRaises(CallFailedPause):
            Retriever(p, Bad(), sleep=lambda s: None).search("P1", 1, "x", 3)

    def test_search_transient_then_pause(self):
        import urllib.error
        p, st = self.demo_project()

        class Down:
            def search(self, q, n):
                raise urllib.error.URLError("down")
        with self.assertRaises(CallFailedPause):
            Retriever(p, Down(), sleep=lambda s: None).search("P1", 1, "x", 3)

    def test_wrong_shape_json_is_format_retry(self):
        p, st = self.demo_project()
        prov = FlakyProvider(['{"searches": ["just a string"]}', '{"searches": []}'])
        hub = ModelHub(p, st, {}, providers={"mock|": prov}, sleep=lambda s: None)
        from guanzhe.util import FormatError

        def val(o):
            for q in o["searches"]:
                if not isinstance(q, dict):
                    raise FormatError("须是对象")
        hub.call_json("P1", "s", "u", call_key="k9", round_=1, validator=val)
        self.assertEqual(len(prov.calls), 2)

    def test_same_series_producers_not_independent(self):
        p, st = self.demo_project()
        st["roles"]["P2"]["series"] = "模拟A"
        hub = ModelHub(p, st, {}, providers=demo.providers(), sleep=lambda s: None)
        Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES))).run()
        c = report.build(p)
        i1 = next(r for r in c["逐条结果"] if r["条目"] == "I01")
        self.assertEqual(i1["独立验证次数"], 1)  # 只计可信网站的独立来源
        self.assertEqual(i1["独立性"], "未独立验证")
        self.assertIn("同一系列", i1["备注"])

    def test_file_hash_verify(self):
        p, st = self.demo_project()
        hub = ModelHub(p, st, {}, providers=demo.providers(), sleep=lambda s: None)
        Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES))).run()
        self.assertEqual(p.verify_files(), [])
        f = next((p.root / "archive").iterdir())
        f.write_text("被改过", encoding="utf-8")
        self.assertTrue(p.verify_files())


class TestGroups(unittest.TestCase):
    def test_report_lists_by_group(self):
        d = Path(tempfile.mkdtemp())
        try:
            cfg = {"project_id": "G", "question": "q", "scope": "s",
                   "items": [{"id": "I01", "name": "甲", "group": "明朝"}, {"id": "I02", "name": "乙", "group": "清朝"}]}
            p = Project.create(d / "g", cfg)
            for it in cfg["items"]:
                p.exec("INSERT INTO items (item_id,name,type,st_check,st_verify,st_indep,st_valid,history)"
                       " VALUES (?,?,?,?,?,?,?,?)", (it["id"], it["name"], "条目", "未查证", "未验证", None, "有效", "[]"))
            c = report.build(p)
            md = (p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertLess(md.index("## 明朝"), md.index("## 清朝"))
            self.assertIn("生肖统计（全部分组合计）", md)
            self.assertEqual([r["分组"] for r in c["逐条结果"]], ["明朝", "清朝"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestFeed(unittest.TestCase):
    def test_demo_feed_readable(self):
        from guanzhe import feed
        d = Path(tempfile.mkdtemp())
        try:
            import subprocess, sys
            subprocess.run([sys.executable, "-m", "guanzhe", "demo", str(d / "demo")], check=True, capture_output=True,
                           cwd=Path(__file__).resolve().parents[1])
            r = feed.messages(d / "demo")
            kinds = {m["kind"] for m in r["messages"]}
            self.assertTrue({"claim", "review_ok", "opinion", "banner"} <= kinds)
            blocked = [m for m in r["messages"] if m.get("check", {}).get("ok") is False]
            self.assertEqual(len(blocked), 1)  # 演示里编造的那一句原句被拦下
            self.assertFalse(any(" P1 " in m["text"] or "{P1}" in m["text"] for m in r["messages"]))
            r2 = feed.messages(d / "demo", after=r["last"])
            self.assertEqual(r2["messages"], [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Test15(Tmp):
    """1.5 新增：干支纪年补年号、找不到的农历原文剔除、检索编号不算数字、缺可信网站提示、窗口改记忆模式。"""

    def flow_with(self, pages):
        p, st = self.demo_project()
        hub = ModelHub(p, st, {}, providers=demo.providers(), sleep=lambda s: None)
        f = Flow(p, st, hub, Retriever(p, MockSearch(pages)))
        rid = f.ret.search("P1", 1, "测试戊帝 出生", 1, item_id="I05")[0]["retrieval_id"]
        return f, rid

    def test_ganzhi_with_supplied_era(self):
        f, rid = self.flow_with([{"url": "https://x.test/sl", "title": "实录（虚构）", "keywords": ["戊帝"],
                                  "text": "测试戊帝，母某氏。以己卯岁二月九日，生上于北京。"}])
        base = {"状态": "查到", "出处": rid, "原句": "以己卯岁二月九日，生上于北京"}
        ok = dict(base, 农历原文="建文元年己卯岁二月九日")
        flags, st = f.check_block("P1", "I05", ok)
        self.assertEqual((st, ok["出生日期"]), ("待审查", "1399-03-16"))
        self.assertTrue(any("年号由生产者补出，干支与历表一致" in x for x in flags))
        self.assertIn("年号由生产者补出", ok["程序换算"])
        bad = dict(base, 农历原文="建文二年己卯岁二月九日")  # 建文二年是庚辰年
        flags, st = f.check_block("P1", "I05", bad)
        self.assertNotEqual(st, "待审查")
        self.assertTrue(any("对不上" in x for x in flags))
        bare = dict(base, 农历原文="己卯岁二月九日")  # 没补年号：提示可以补
        flags, st = f.check_block("P1", "I05", bare)
        self.assertTrue(any("只有干支纪年" in x for x in flags))
        self.assertIn("农历原文", bare)

    def test_lunar_not_in_source_is_removed(self):
        f, rid = self.flow_with([{"url": "https://x.test/a", "title": "某页（虚构）", "keywords": ["戊帝"],
                                  "text": "公元1399年3月16日，测试戊帝出生了。"}])
        fx = {"状态": "查到", "出处": rid, "原句": "公元1399年3月16日，测试戊帝出生了", "出生日期": "1399-03-16",
              "农历原文": "建文元年（己卯年）二月（丁卯月）初九日（某日）"}
        flags, st = f.check_block("P1", "I05", fx)
        self.assertNotIn("农历原文", fx)
        self.assertIn("农历原文未在原文中找到（疑为模型记忆），已剔除", flags)
        self.assertEqual(st, "待审查")  # 公历日期本身在原句里，照常交审查

    def test_lunar_with_parenthesis_in_source_kept(self):
        f, rid = self.flow_with([{"url": "https://x.test/b", "title": "某页（虚构）", "keywords": ["戊帝"],
                                  "text": "测试戊帝生于建文元年（1399年）二月初九日。"}])
        fx = {"状态": "查到", "出处": rid, "原句": "测试戊帝生于建文元年（1399年）二月初九日",
              "农历原文": "建文元年二月初九日"}
        flags, st = f.check_block("P1", "I05", fx)
        self.assertEqual((st, fx["出生日期"], fx["农历原文"]), ("待审查", "1399-03-16", "建文元年二月初九日"))

    def test_ids_not_counted_as_numbers(self):
        self.assertEqual(B.flag_notes("依据 S00130、S00123，并参考 I05 与 P1-R2-B003"), [])
        self.assertTrue(B.flag_notes("我认为是 1399 年")[0].startswith("说明文字中出现数字"))

    def test_window_saves_memory_keeps_other_fields(self):
        from guanzhe.app import App
        src = Path(__file__).resolve().parents[1] / "examples" / "设置.json"
        (self.dir / "设置.json").write_bytes(src.read_bytes())
        before = json.loads(src.read_text(encoding="utf-8-sig"))
        a = App(self.dir)
        r = a.set_memory({"P1": {"mode": "全局"}, "P2": {"mode": "固定", "see": ["self", "P1"], "rounds": 2},
                          "R1": {"mode": "全新"}})
        self.assertTrue(r["ok"], r)
        self.assertEqual((self.dir / "设置.json.bak").read_bytes(), src.read_bytes())
        after = json.loads((self.dir / "设置.json").read_text(encoding="utf-8"))
        self.assertEqual(after["roles"]["P2"]["memory"], {"mode": "固定", "see": ["self", "P1"], "rounds": 2})
        for x in (before, after):
            for v in x["roles"].values():
                v.pop("memory", None)
        self.assertEqual(before, after)  # 其他字段原样保留
        self.assertFalse(a.set_memory({"R1": {"mode": "固定"}})["ok"])  # 审查者只有 全局/全新
        self.assertFalse(a.set_memory({"P1": {"mode": "固定", "see": [], "rounds": 1}})["ok"])
        self.assertFalse(a.set_memory({"P1": {"mode": "固定", "see": ["self"], "rounds": 99}})["ok"])
        self.assertFalse(a.set_memory({"P9": {"mode": "全局"}})["ok"])

    def test_missing_trusted_sites_warned(self):
        from guanzhe import feed
        from guanzhe.app import App
        cfg = demo.project_config()
        cfg.pop("trusted_sites", None)
        p = Project.create(self.dir / "无名单", cfg)
        self.assertFalse(App(self.dir).trusted_ok("无名单"))
        p.log("程序", "缺可信网站名单", content={"说明": "警告"})
        kinds = [m["kind"] for m in feed.messages(self.dir / "无名单")["messages"]]
        self.assertIn("alert", kinds)
        (self.dir / "q.json").write_text(json.dumps({"question": "q", "items": []}), encoding="utf-8")
        self.assertEqual(App(self.dir).questions()[0]["trusted"], False)
        self.assertFalse((Path(__file__).resolve().parents[1] / "examples" / "明朝皇帝生肖.json").exists())


class Test16(Tmp):
    """1.6 新增：朝代提示不排除前朝年号、独立来源编号只认程序提供的、同样内容反复退回时停下。"""

    def test_calendar_previous_dynasty_era(self):
        from guanzhe import cn_calendar as C
        self.assertEqual(C.from_text("元文宗天曆元年九月十八日", "明朝")[1]["date"], "1328-10-21")  # 朱元璋生于元
        self.assertEqual(C.from_text("明嘉靖三十八年四月初八日", "清朝")[1]["date"], "1559-05-14")  # 努尔哈赤生于明
        self.assertEqual(C.from_text("建文元年二月初九日", "明朝")[1]["date"], "1399-03-16")

    def _run_with_reviewer_source(self, rewrite):
        import json as _j
        p, st = self.demo_project()
        router = demo.providers()["mock|"]
        orig = router.map["mock-c"]

        def patched(model, system, user):
            o = _j.loads(orig(model, system, user))
            for x in o["reviews"]:
                if x.get("independent") == "支持":
                    x["independent_source"] = rewrite(x["independent_source"])
            return _j.dumps(o, ensure_ascii=False)
        router.map["mock-c"] = patched
        hub = ModelHub(p, st, {}, providers={"mock|": router}, sleep=lambda s: None)
        Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES))).run()
        return p

    def test_multiple_source_ids_accepted(self):
        p = self._run_with_reviewer_source(lambda s: f"{s}, S99999")  # 多写一个不存在的编号
        row = p.q("SELECT value, indep_count, note FROM items WHERE item_id='I01'")[0]
        self.assertIn("trusted.test", row["note"])
        self.assertGreaterEqual(row["indep_count"], 1)
        srcs = {r["indep_source"] for r in p.q("SELECT indep_source FROM reviews WHERE indep_check='支持'")}
        self.assertFalse(any("S99999" in (s or "") for s in srcs))

    def test_unoffered_source_not_support(self):
        p = self._run_with_reviewer_source(lambda s: "S99999")  # 只写了一个程序没提供过的编号
        self.assertFalse(p.q("SELECT 1 FROM reviews WHERE indep_check='支持'"))
        note = p.q("SELECT note FROM items WHERE item_id='I01'")[0]["note"]
        self.assertNotIn("独立支持", note)

    def test_stalled_item_stops(self):
        p, st = self.demo_project()
        f = Flow(p, st, None, None)
        fields = json.dumps({"状态": "查到", "出处": "S00001", "原句": "x", "出生日期": "", "农历原文": "某年", "异说": "a"},
                            ensure_ascii=False)
        fields2 = json.dumps({"状态": "查到", "出处": "S00001", "原句": "x", "出生日期": "", "农历原文": "某年", "异说": "a；b"},
                             ensure_ascii=False)
        flags = json.dumps(["历表中查不到这个农历日期"], ensure_ascii=False)
        for r, fs in ((1, fields), (2, fields2)):
            p.exec("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (f"P1-R{r}-B001", "A00001", "P1", r, "条目", "I01", fs, "未查证", "未验证", None, "有效", "[]", flags))
        self.assertTrue(f._stalled("P1", "I01", 2))  # 只是异说多了几个字，不算实质改动
        self.assertFalse(f._stalled("P1", "I01", 1))


class Test17(unittest.TestCase):
    """1.7：日期字段核对器独立可用；同样的输入每次给出同样的摘录（与随机种子无关）。"""

    def test_date_field_standalone(self):
        from guanzhe.fields import DateField
        F = DateField(key="出生日期", noun="出生")
        self.assertEqual(F.agree([(1328, 10, 21), (1328, 10, 29)]), (1328, 10, 21))
        self.assertEqual(F.format(F.normalize("1399")), "1399")
        f = {"原句": "生于建文元年二月初九日", "农历原文": "建文元年二月初九日"}
        flags, st = F.check(f, "测试：生于建文元年二月初九日。", "明朝")
        self.assertEqual((st, f["出生日期"], f["农历年干支"]), ("待审查", "1399-03-16", "己卯"))
        self.assertIn("出生", " ".join(F.check({"原句": "无日期", "出生日期": "abc"}, "无日期", None)[0]))
        self.assertEqual(F.category([{}], (1399, 3, 16), "明朝"), "己卯")

    def test_excerpts_deterministic(self):
        import os, subprocess, sys
        code = ("from guanzhe.tasks.fact_compilation import aliases, birth_windows;"
                "k=aliases({'name':'明宣宗朱瞻基（宣德）','aliases':['宣宗','瞻基']});"
                "t='宣宗朱瞻基生于建文元年二月初九日。'*3+'瞻基，1399年生。';"
                "print(k, birth_windows(t,k,width=30,top=3))")
        outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               cwd=Path(__file__).resolve().parents[1],
                               env=dict(os.environ, PYTHONHASHSEED=str(s))).stdout for s in (1, 2, 3, 4)}
        self.assertEqual(len(outs), 1)


class Test18(Tmp):
    """1.8：诺贝尔奖题包（得主名单＋获奖原因）用模拟模型从头跑通；名字比对忽略变音符号与中间名缩写；机构得主。"""

    PAGES = [
        {"url": "https://www.nobelprize.org/prizes/medicine/2022/summary/", "title": "Medicine 2022", "keywords": ["医学奖", "Medicine"],
         "text": "The Nobel Prize in Physiology or Medicine 2022 was awarded to Svante Pääbo \"for his discoveries concerning "
                 "the genomes of extinct hominins and human evolution.\""},
        {"url": "https://example.test/med2022", "title": "某新闻", "keywords": ["医学奖", "Medicine"],
         "text": "2022年诺贝尔生理学或医学奖：The Nobel Prize in Physiology or Medicine 2022 was awarded to Svante Paabo "
                 "for his discoveries concerning the genomes of extinct hominins and human evolution."},
        {"url": "https://www.nobelprize.org/prizes/peace/2020/summary/", "title": "Peace 2020", "keywords": ["和平奖", "Peace"],
         "text": "The Nobel Peace Prize 2020 was awarded to World Food Programme (WFP) \"for its efforts to combat hunger, "
                 "for its contribution to bettering conditions for peace in conflict-affected areas and for acting as a driving "
                 "force in efforts to prevent the use of hunger as a weapon of war and conflict.\""},
        {"url": "https://example.test/peace2020", "title": "某百科", "keywords": ["和平奖", "Peace"],
         "text": "The Nobel Peace Prize 2020 was awarded to the World Food Programme for its efforts to combat hunger, "
                 "for its contribution to bettering conditions for peace in conflict-affected areas and for acting as a driving "
                 "force in efforts to prevent the use of hunger as a weapon of war and conflict."},
        {"url": "https://zh.wikipedia.org/wiki/x", "title": "维基", "keywords": ["医学奖", "Medicine", "和平奖", "Peace"], "hidden": True,
         "text": "2022年诺贝尔生理学或医学奖授予 Svante Pääbo，for his discoveries concerning the genomes of extinct hominins "
                 "and human evolution。2020年诺贝尔和平奖授予 World Food Programme，for its efforts to combat hunger, for its "
                 "contribution to bettering conditions for peace in conflict-affected areas and for acting as a driving force in "
                 "efforts to prevent the use of hunger as a weapon of war and conflict。"},
    ]
    CFG = {"project_id": "NB", "task": "nobel", "question": "q", "scope": "s",
           "trusted_sites": ["nobelprize.org", "zh.wikipedia.org"],
           "items": [{"id": "N01", "name": "2022年诺贝尔生理学或医学奖", "group": "生理学或医学奖", "year": 2022,
                      "aliases": ["Nobel Prize in Physiology or Medicine 2022", "2022年诺贝尔生理学或医学奖"],
                      "query": "Nobel Prize in Physiology or Medicine 2022"},
                     {"id": "N02", "name": "2020年诺贝尔和平奖", "group": "和平奖", "year": 2020,
                      "aliases": ["Nobel Peace Prize 2020", "2020年诺贝尔和平奖"], "query": "Nobel Peace Prize 2020"}]}

    class Router:
        def __init__(self, which):
            self.which = which

        def complete(self, model, system, user, max_tokens, timeout):
            import json as _j
            if "请为需要检索的条目给出检索词" in user:
                its = re.findall(r"- (N\d+)：", user)
                q = {"N01": "诺贝尔生理学或医学奖 Medicine 2022", "N02": "诺贝尔和平奖 Peace 2020"}
                out = {"searches": [{"item": i, "query": q[i]} for i in its]}
            elif '"reviews"' in user:
                rows = [_j.loads(l) for l in user.splitlines() if l.startswith("{") and '"block"' in l]
                out = {"reviews": [{"block": b["block"], "equivalent": "等价",
                                    "independent": "支持" if isinstance(b.get("独立来源"), list) else "无独立来源",
                                    "independent_source": b["独立来源"][0]["编号"] if isinstance(b.get("独立来源"), list) else "",
                                    "reason": "一致"} for b in rows]}
            else:
                blocks = []
                for sec in re.split(r"==== 条目 ", user)[1:]:
                    iid = sec[:3]
                    found = re.findall(r"【(S\d+)】[^\n]*\n([^【=]*)", sec)
                    pick = found[0] if self.which == "P1" else found[-1]
                    rid, body = pick
                    sent = re.search(r"The Nobel[^\n]*", body).group(0).strip()
                    if iid == "N01":
                        names = "Svante Pääbo" if self.which == "P1" else "Svante Paabo"
                        cit = "for his discoveries concerning the genomes of extinct hominins and human evolution"
                    else:
                        names = "World Food Programme (WFP)" if self.which == "P1" else "World Food Programme"
                        cit = re.search(r"for its efforts.*conflict", sent).group(0)
                    blocks.append({"type": "条目", "item": iid, "fields": {
                        "奖项": iid, "得主": names, "获奖原因": cit, "中文参考": "（译文）", "状态": "查到",
                        "出处": rid, "原句": sent}})
                out = {"blocks": blocks, "notes": "", "changes": [], "responses": []}
            t = _j.dumps(out, ensure_ascii=False)
            return t, len(user) // 2, len(t) // 2

    def test_nobel_end_to_end(self):
        p = Project.create(self.dir / "nb", self.CFG)
        st = load_settings_from_obj(demo.settings())
        for r, m in (("P1", "mock-a"), ("P2", "mock-b"), ("R1", "mock-c")):
            st["roles"][r]["model"] = m
        routers = {"mock-a": self.Router("P1"), "mock-b": self.Router("P2"), "mock-c": self.Router("R1")}

        class Pick:
            def complete(self, model, *a, **k):
                return routers[model].complete(model, *a, **k)
        hub = ModelHub(p, st, {}, providers={"mock|": Pick()}, sleep=lambda s: None)
        res = Flow(p, st, hub, Retriever(p, MockSearch(self.PAGES))).run()
        self.assertEqual(res, "完成")
        c = report.build(p)
        rows = {r["条目"]: r for r in c["逐条结果"]}
        self.assertEqual(rows["N01"]["结论"], "查到", rows["N01"]["备注"])
        self.assertEqual(rows["N02"]["结论"], "查到", rows["N02"]["备注"])
        self.assertIn("Svante", rows["N01"]["得主"])
        for iid in ("N01", "N02"):  # 独立核验确实走了可信网站（生产者没引用的 zh.wikipedia.org）
            self.assertGreaterEqual(rows[iid]["独立验证次数"], 1, rows[iid]["备注"])
            self.assertIn("zh.wikipedia.org", rows[iid]["备注"])
        self.assertEqual(rows["N02"]["得主人数"], 1)
        md = (self.dir / "nb" / "outputs" / "结论.md").read_text(encoding="utf-8")
        self.assertIn("## 和平奖", md)
        from guanzhe import feed
        msgs = feed.messages(self.dir / "nb")["messages"]
        self.assertTrue(any("获奖原因" in m["text"] for m in msgs if m["kind"] == "claim"))

    def test_name_and_citation_checkers(self):
        from guanzhe.fields.record import RecordField
        F = RecordField()
        raw = 'The Nobel Prize in Physics 2016 was awarded to David J. Thouless, F. Duncan M. Haldane and J. Michael Kosterlitz ' \
              '"for theoretical discoveries of topological phase transitions and topological phases of matter."'
        f = {"得主": "David Thouless；F. Duncan M. Haldane；J. Michael Kosterlitz",
             "获奖原因": "for theoretical discoveries of topological phase transitions and topological phases of matter"}
        self.assertEqual(F.check(f, raw)[1], "待审查")
        bad = dict(f, 得主="David Thouless；Someone Else")
        fl, st = F.check(bad, raw)
        self.assertEqual(st, "未查证")
        self.assertIn("Someone Else", fl[0])
        a = F.normalize(F.value(f))
        b = F.normalize(F.value(dict(f, 得主="J. Michael Kosterlitz；David J. Thouless；Duncan Haldane")))
        self.assertIsNotNone(F.agree([a, b]))  # 顺序、中间名缩写不同也算一致
        c = F.normalize(F.value(dict(f, 得主="David Thouless；Duncan Haldane")))
        self.assertIsNone(F.agree([a, c]))  # 少一位得主不一致
        self.assertEqual(F.check(dict(f, 获奖原因="for discoveries in physics"), raw)[1], "未查证")


class Test19(Tmp):
    """1.9：固定的数据文件夹（从旁边的旧版本复制设置、密钥、题目、项目）；窗口直接用 examples 里的题目。"""

    def test_data_folder_migration_and_examples(self):
        import shutil, subprocess, sys, time
        from guanzhe import app as A
        prog_src = Path(__file__).resolve().parents[1]
        parent = self.dir / "原型"
        new = parent / "观者自在(AlreadyThere)_v1.9"
        old = parent / "观者自在(AlreadyThere)_v1.8"
        shutil.copytree(prog_src / "guanzhe", new / "guanzhe")
        shutil.copytree(prog_src / "examples", new / "examples")
        (old / "guanzhe").mkdir(parents=True)
        (old / "guanzhe" / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(prog_src / "examples" / "设置.json", old / "设置.json")
        (old / "密钥.env").write_text("DEEPSEEK_API_KEY=x\n", encoding="utf-8")
        (old / "我的题.json").write_text(json.dumps({"question": "q", "items": []}), encoding="utf-8")
        Project.create(old / "旧项目", demo.project_config()).close()
        data = parent / "数据"
        moved = A.migrate(new, data)
        for x in ("设置.json", "密钥.env", "我的题.json", "旧项目/project.db"):
            self.assertTrue((data / x).exists(), x)
        self.assertTrue((old / "旧项目" / "project.db").exists())  # 原处不删
        self.assertEqual(A.migrate(new, data), [])  # 第二次不重复复制
        self.assertTrue(len(moved) >= 4 and (data / "搬迁记录.txt").exists())
        a = A.App(data, new, moved)
        qs = {q["file"] for q in a.questions()}
        self.assertIn("examples/近十年诺贝尔奖.json", qs)
        self.assertIn("我的题.json", qs)
        r = a.new_project("诺奖项目", "examples/近十年诺贝尔奖.json")  # 子进程在数据文件夹里跑，也能找到程序
        self.assertTrue(r["ok"], r)
        self.assertTrue((data / "诺奖项目" / "project.db").exists())
        self.assertIn("旧项目", {p["name"] for p in a.projects()})
        self.assertFalse(a.new_project("x", "examples/../设置.json")["ok"])

    def test_split_citation_parts(self):
        from guanzhe.fields.record import RecordField
        F = RecordField()
        raw = ('The Nobel Prize in Physics 2020 was divided, one half awarded to Roger Penrose "for the discovery that black hole '
               'formation is a robust prediction of the general theory of relativity", the other half jointly to Reinhard Genzel '
               'and Andrea Ghez "for the discovery of a supermassive compact object at the centre of our galaxy."')
        f = {"得主": "Roger Penrose；Reinhard Genzel；Andrea Ghez",
             "获奖原因": "for the discovery that black hole formation is a robust prediction of the general theory of relativity；"
                        "for the discovery of a supermassive compact object at the centre of our galaxy"}
        self.assertEqual(F.check(f, raw)[1], "待审查")
        g = dict(f, 获奖原因="for the discovery of a supermassive compact object at the centre of our galaxy; "
                           "for the discovery that black hole formation is a robust prediction of the general theory of relativity")
        self.assertIsNotNone(F.agree([F.normalize(F.value(f)), F.normalize(F.value(g))]))  # 顺序不同也一致
        h = dict(f, 获奖原因="for the discovery of a supermassive compact object at the centre of our galaxy")
        self.assertIsNone(F.agree([F.normalize(F.value(f)), F.normalize(F.value(h))]))  # 少一段不一致
        self.assertEqual(F.check(dict(f, 获奖原因="for black holes；for galaxies"), raw)[1], "未查证")

    def test_partial_batch_accepted(self):
        import json as _j
        p, st = self.demo_project()
        router = demo.providers()["mock|"]
        orig = router.map["mock-a"]

        def drop_one(model, system, user):
            o = _j.loads(orig(model, system, user))
            if "blocks" in o and "第 1 轮" in user:
                o["blocks"] = o["blocks"][1:] + o["blocks"][:1]   # 顺序打乱
                o["blocks"].append(dict(o["blocks"][0]))          # 多一个重复块
                o["blocks"] = [b for b in o["blocks"] if b["item"] != "I02"]  # 漏交 I02
            return _j.dumps(o, ensure_ascii=False)
        router.map["mock-a"] = drop_one
        hub = ModelHub(p, st, {}, providers={"mock|": router}, sleep=lambda s: None)
        Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES))).run()
        ev = p.q("SELECT content FROM events WHERE type='部分提交'")
        self.assertTrue(ev and "I02" in ev[0]["content"])
        self.assertFalse(p.q("SELECT 1 FROM calls WHERE call_id LIKE 'R1-P1-write%' AND status!='成功'"))  # 没有整批重交
