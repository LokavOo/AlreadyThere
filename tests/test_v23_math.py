"""2.3：答案格式校验、数值验证、小数近似判错。"""
import unittest

from alreadythere.fields import mathcheck as MC
from alreadythere.fields.answer import compare, compare_detail
from alreadythere.tasks import final_answer as T
from alreadythere.util import FormatError

GOLD_SAMPLES = [r"\frac{(2m)!!}{(2m+1)!!}", r"n - 1 - \lfloor \frac{t\cdot n}{2t+1} \rfloor", r"\binom{N+1}{3}",
                r"\max(3-p, 2/p)", r"\min(k/2,d)", r"(2(1-\rho))^n", r"\binom{n-3}{2}+4", r"\lfloor \frac{n}{2} \rfloor",
                r"p-1-2\left\lfloor\frac{p}{r+1}\right\rfloor", r"\frac{1}{2}", r"(-\infty, 1]", r"1\pm\sqrt{2}"]


class TestMath(unittest.TestCase):
    def test_gold_samples_parse(self):
        for g in GOLD_SAMPLES:
            ok, info = MC.can_parse(g)
            self.assertTrue(ok, (g, info))

    def test_unicode_vs_latex(self):
        self.assertEqual(compare("5√2", r"5\sqrt{2}"), "自动一致")
        self.assertEqual(compare("π/4", r"\frac{\pi}{4}"), "自动一致")
        self.assertEqual(compare("x²+1", "x^{2}+1"), "自动一致")
        self.assertEqual(compare("3×4", "12"), "自动一致")

    def test_numeric_equivalence(self):
        for a, b in [(r"\frac{\sqrt3}{2}", r"\sin\frac{\pi}{3}"), (r"2\sqrt2", r"\sqrt8"), (r"\ln 8", r"3\ln 2"),
                     (r"(x+1)^2", r"x^2+2x+1"), (r"\binom{n}{2}", r"\frac{n(n-1)}{2}"), ("2^{10}", "1024"),
                     (r"\arctan 1", r"\frac{\pi}{4}"), (r"1\pm\sqrt2", r"\{1-\sqrt2,1+\sqrt2\}")]:
            self.assertEqual(compare(a, b), "自动一致", (a, b))
        for a, b in [(r"\sqrt2", r"\sqrt3"), (r"x^2", r"x^3"), (r"\frac{\pi}{4}", r"\frac{\pi}{3}")]:
            self.assertEqual(compare(a, b), "不一致", (a, b))

    def test_precision_beyond_double(self):
        # 双精度分不出来的差别（1e-20 量级）也要分得出
        self.assertEqual(compare(r"\sqrt{2}", "1.41421356237309504880168872420969807856967"), "不一致")
        self.assertEqual(compare(r"1+10^{-20}", "1"), "不一致")

    def test_structure(self):
        self.assertEqual(compare("(-\\infty,1)", "(-\\infty,1]"), "不一致")
        self.assertEqual(compare("(1,2)", "(1,2)"), "自动一致")
        self.assertEqual(compare("(1,2,3)", "(1,3,2)"), "不一致")
        self.assertEqual(compare(r"\{1,2,3\}", r"\{3,1,2\}"), "自动一致")
        self.assertEqual(compare(r"(-\infty,1)\cup(2,\infty)", r"(2,\infty)\cup(-\infty,1)"), "自动一致")

    def test_decimal_approx_is_wrong(self):
        d = compare_detail("1.414", r"\sqrt{2}")
        self.assertEqual((d["result"], d["how"], d["layer"]), ("不一致", "小数近似≠精确值", "表示层"))
        self.assertEqual(compare("0.333", r"\frac13"), "不一致")
        self.assertEqual(compare("3.14", r"\pi"), "不一致")
        self.assertEqual(compare("0.5", r"\frac12"), "自动一致")      # 有限小数等于它的分数
        self.assertEqual(compare("2.0", "2"), "自动一致")
        # 标准答案本身是近似：按标准答案精度比
        d = compare_detail(r"\sqrt{2}", "1.414", b_is_gold=True)
        self.assertEqual((d["result"], d["how"]), ("自动一致", "按标准答案精度"))
        self.assertEqual(compare(r"\sqrt{3}", "1.414", b_is_gold=True), "不一致")

    def test_conservative(self):
        self.assertEqual(compare("x=3", "y=3"), "待人工判断")
        self.assertEqual(compare(r"\text{yes}", "1"), "待人工判断")
        self.assertEqual(compare("", "1"), "待人工判断")

    def test_sympy_field_fallback(self):
        # LaTeX 程序读不了（\sum），退回 sympy 写法
        self.assertEqual(compare(r"\sum_{k=1}^{3}k", "6", a_py="1+2+3"), "自动一致")
        self.assertEqual(compare(r"\sum_{k=1}^{3}k", "6"), "待人工判断")

    def test_py_parse(self):
        self.assertTrue(str(MC.num_value("sqrt(2)*pi")).startswith("4.44288293815836624"))
        with self.assertRaises(MC.MathError):
            MC.parse_py("2^3")
        with self.assertRaises(MC.MathError):
            MC._check_eval(MC.parse_py("__import__('os').system('x')"))  # 求值器只认白名单，绝不执行任意代码


class TestFormat(unittest.TestCase):
    """2.4：格式类一律宽收——整理、记警告，不打回；只有真的无法使用（答案为空等）才打回。"""

    def sub(self, **kw):
        o = {"status": "得出", "answer": r"\sqrt{2}", "sympy": "sqrt(2)", "reasoning": "r",
             "verify_plan": {"what": "w", "method": "m", "expected": "e", "tolerance": "无"}, "code": "", "limits": ""}
        o.update(kw)
        return o

    def check(self, **kw):
        o = self.sub(**kw)
        T.validate_submission()(o)
        return o

    def test_ok(self):
        self.check()
        self.check(sympy="")
        self.check(answer=r"(-\infty, 1]", sympy="")
        self.check(answer=r"\sin(x)", sympy="")

    def test_tidied_not_rejected(self):
        o = self.check(answer="5√2", sympy="")
        self.assertEqual(o["answer"], r"5\sqrt{2}")
        self.assertTrue(any("整理" in w for w in o["warnings"]))
        self.assertEqual(self.check(answer="$5$", sympy="")["answer"], "5")
        self.assertEqual(self.check(answer=r"\boxed{5}", sympy="")["answer"], "5")
        self.assertEqual(self.check(answer="π", sympy="")["answer"].strip(), r"\pi")
        o = self.check(answer="sqrt(2)", sympy="")
        self.assertTrue(any("程序写法" in w for w in o["warnings"]))

    def test_empty_still_rejected(self):
        with self.assertRaises(FormatError):
            self.check(answer="", sympy="")

    def test_sympy_only_warns(self):
        o = self.check(sympy="2^3")
        self.assertEqual(o["sympy"], "")
        self.assertTrue(any("读不了" in w for w in o["warnings"]))
        o = self.check(sympy="sqrt(3)")
        self.assertTrue(any("不一致" in w for w in o["warnings"]))
        o = self.check(sympy="foo(1)")
        self.assertEqual(o["sympy"], "")

    def test_plain_answer_falls_back_to_sympy(self):
        # 答案写成 sqrt(2)：LaTeX 读不了（宁可不读也不误读成变量相乘），比对时退回 sympy 字段
        self.assertEqual(compare("sqrt(2)", r"\sqrt{2}"), "待人工判断")
        self.assertEqual(compare("sqrt(2)", r"\sqrt{2}", a_py="sqrt(2)"), "自动一致")


if __name__ == "__main__":
    unittest.main()
