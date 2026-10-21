"""数学答案的解析与数值验证（2.3）。不依赖 SymPy/mpmath：只用标准库的 decimal，精度 60 位，
比较时按 30 位有效数字。

做三件事：
1. parse_latex(s)：把严格的 LaTeX 答案解析成结构（数/表达式、区间或元组、集合、并集）；
2. parse_py(s)：把生产者另交的"sympy 写法"（sqrt(2)*pi 这类 Python 语法）解析成同样的结构；
3. num_equal / struct_equal：数值比较（含变量的表达式在若干随机点上代入比较）、结构比较。

任何一步解析不了、算不出来，都返回 None / 抛 MathError，调用方一律落到"待人工判断"，不瞎猜。
"""

from __future__ import annotations

import ast
import math
import random
import re
from decimal import Decimal, InvalidOperation, localcontext, getcontext, ROUND_HALF_EVEN

PREC = 60
TOL_DIGITS = 30


class MathError(Exception):
    pass


# ------------------------------------------------------------------ Unicode → LaTeX

_UNI = {
    "−": "-", "–": "-", "—": "-", "×": r"\times ", "·": r"\cdot ", "÷": r"\div ", "π": r"\pi ", "∞": r"\infty ",
    "≤": r"\le ", "≥": r"\ge ", "≠": r"\ne ", "∪": r"\cup ", "∩": r"\cap ", "±": r"\pm ", "∈": r"\in ",
    "θ": r"\theta ", "α": r"\alpha ", "β": r"\beta ", "γ": r"\gamma ", "λ": r"\lambda ", "μ": r"\mu ",
    "φ": r"\varphi ", "ϕ": r"\varphi ", "ε": r"\epsilon ", "σ": r"\sigma ", "ω": r"\omega ", "δ": r"\delta ",
    "⁻": "^{-}", "∅": r"\emptyset ",
}
_SUP = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}
_SUB = {"₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9"}
_VULGAR = {"½": r"\frac{1}{2}", "⅓": r"\frac{1}{3}", "⅔": r"\frac{2}{3}", "¼": r"\frac{1}{4}", "¾": r"\frac{3}{4}"}
UNICODE_MATH = set(_UNI) | set(_SUP) | set(_SUB) | set(_VULGAR) | {"√", "∛", "∜"}


def has_unicode_math(s: str) -> str | None:
    """返回答案里出现的第一个 Unicode 数学符号，没有则 None（用于"必须写 LaTeX"的格式校验）。"""
    for ch in s:
        if ch in UNICODE_MATH:
            return ch
    return None


def uni_to_latex(s: str) -> str:
    """Unicode 数学符号 → LaTeX（√ π ² × − ≤ 等）。普通 ASCII LaTeX 原样不动。"""
    s = s.replace("−", "-")
    # 上标连续数字：x²³ → x^{23}
    s = re.sub("[⁰¹²³⁴⁵⁶⁷⁸⁹]+", lambda m: "^{" + "".join(_SUP[c] for c in m.group()) + "}", s)
    s = re.sub("[₀₁₂₃₄₅₆₇₈₉]+", lambda m: "_{" + "".join(_SUB[c] for c in m.group()) + "}", s)
    for k, v in _VULGAR.items():
        s = s.replace(k, v)
    # 根号：√(...)、√数字/字母/\pi
    def sq(sym, idx):
        nonlocal s
        out, i = [], 0
        while i < len(s):
            if s[i] != sym:
                out.append(s[i]); i += 1; continue
            i += 1
            while i < len(s) and s[i] == " ":
                i += 1
            if i < len(s) and s[i] == "(":
                d, j = 0, i
                while j < len(s):
                    d += {"(": 1, ")": -1}.get(s[j], 0)
                    if d == 0:
                        break
                    j += 1
                arg, i = s[i + 1:j], j + 1
            else:
                m = re.match(r"\d+(\.\d+)?|[A-Za-z]|\\[A-Za-z]+", s[i:])
                if not m:
                    out.append(sym); continue
                arg, i = m.group(), i + len(m.group())
            out.append(f"\\sqrt[{idx}]{{{arg}}}" if idx else f"\\sqrt{{{arg}}}")
        s = "".join(out)
    sq("√", None); sq("∛", 3); sq("∜", 4)
    for k, v in _UNI.items():
        s = s.replace(k, v)
    return s


# ------------------------------------------------------------------ 高精度函数（Decimal）

def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(x)


def _pi():
    def atan_inv(n):
        x = Decimal(1) / n
        t, s, k, x2 = x, x, 1, x * x
        while abs(t) > Decimal(10) ** (-PREC - 5):
            t = -t * x2
            k += 2
            s += t / k
        return s
    return 4 * (4 * atan_inv(5) - atan_inv(239))


_PI = None


def pi():
    global _PI
    if _PI is None:
        with localcontext() as c:
            c.prec = PREC + 10
            _PI = _pi()
    return +_PI


def _sin_cos(x):
    x = D(x)
    twopi = 2 * pi()
    x = x - twopi * (x / twopi).to_integral_value(rounding="ROUND_FLOOR")
    s, c, t, k = Decimal(0), Decimal(0), Decimal(1), 0
    x2 = x * x
    # 逐项泰勒
    ts = Decimal(1)
    sin_v, cos_v = Decimal(0), Decimal(0)
    term = Decimal(1)
    n = 0
    while True:
        if n % 4 == 0: cos_v += term
        elif n % 4 == 1: sin_v += term
        elif n % 4 == 2: cos_v -= term
        else: sin_v -= term
        n += 1
        term = term * x / n
        if abs(term) < Decimal(10) ** (-PREC - 5) and n > 5:
            break
        if n > 2000:
            raise MathError("三角函数级数不收敛")
    return sin_v, cos_v


def f_sin(x): return _sin_cos(x)[0]
def f_cos(x): return _sin_cos(x)[1]


def f_tan(x):
    s, c = _sin_cos(x)
    if abs(c) < Decimal(10) ** -40:
        raise MathError("tan 无定义")
    return s / c


def f_atan(x):
    x = D(x)
    if x < 0:
        return -f_atan(-x)
    if x > 1:
        return pi() / 2 - f_atan(1 / x)
    # 减半：atan(x) = 2 atan(x / (1 + sqrt(1+x²)))
    k = 0
    while x > Decimal("0.1"):
        x = x / (1 + (1 + x * x).sqrt())
        k += 1
    t, s, n, x2 = x, x, 1, x * x
    while abs(t) > Decimal(10) ** (-PREC - 5):
        t = -t * x2
        n += 2
        s += t / n
    return s * (2 ** k)


def f_asin(x):
    x = D(x)
    if abs(x) > 1:
        raise MathError("asin 定义域")
    if abs(x) == 1:
        return x * pi() / 2
    return f_atan(x / (1 - x * x).sqrt())


def f_acos(x):
    return pi() / 2 - f_asin(x)


def f_exp(x): return D(x).exp()


def f_ln(x):
    x = D(x)
    if x <= 0:
        raise MathError("ln 定义域")
    return x.ln()


def f_log(b, x):
    return f_ln(x) / f_ln(b)


def f_sqrt(x):
    x = D(x)
    if x < 0:
        raise MathError("负数开方（复数）")
    return x.sqrt()


def f_root(x, n):
    x, n = D(x), D(n)
    if n != n.to_integral_value() or n == 0:
        raise MathError("根指数不是整数")
    ni = int(n)
    if x < 0:
        if ni % 2 == 0:
            raise MathError("负数偶次方根")
        return -f_root(-x, n)
    if x == 0:
        return Decimal(0)
    return (x.ln() / n).exp()


def f_pow(a, b):
    a, b = D(a), D(b)
    if b == b.to_integral_value() and abs(b) <= 100000:
        return a ** int(b)
    if a < 0:
        # 分数指数：试 p/q 且 q 为奇数
        raise MathError("负数的非整数次幂")
    if a == 0:
        if b > 0:
            return Decimal(0)
        raise MathError("0 的非正次幂")
    return (b * a.ln()).exp()


def f_fact(n):
    n = D(n)
    if n != n.to_integral_value() or n < 0 or n > 5000:
        raise MathError("阶乘参数不合法")
    return Decimal(math.factorial(int(n)))


def f_dfact(n):
    n = D(n)
    if n != n.to_integral_value() or n < -1 or n > 20000:
        raise MathError("双阶乘参数不合法")
    r, i = 1, int(n)
    while i > 1:
        r *= i
        i -= 2
    return Decimal(r)


def f_binom(n, k):
    n, k = D(n), D(k)
    if n != n.to_integral_value() or k != k.to_integral_value() or n < 0 or n > 100000:
        raise MathError("二项式系数参数不合法")
    n, k = int(n), int(k)
    if k < 0 or k > n:
        return Decimal(0)
    return Decimal(math.comb(n, k))


def f_sinh(x): x = D(x); return (x.exp() - (-x).exp()) / 2
def f_cosh(x): x = D(x); return (x.exp() + (-x).exp()) / 2
def f_tanh(x): return f_sinh(x) / f_cosh(x)


FUNCS = {
    "sqrt": f_sqrt, "root": f_root, "exp": f_exp, "ln": f_ln, "log": f_ln, "logb": f_log, "sin": f_sin, "cos": f_cos,
    "tan": f_tan, "atan": f_atan, "arctan": f_atan, "asin": f_asin, "arcsin": f_asin, "acos": f_acos,
    "arccos": f_acos, "sinh": f_sinh, "cosh": f_cosh, "tanh": f_tanh, "abs": lambda x: abs(D(x)),
    "binomial": f_binom, "factorial": f_fact, "Rational": lambda a, b: D(a) / D(b),
    "dfactorial": lambda n: f_dfact(n), "max": lambda *a: max(D(x) for x in a), "min": lambda *a: min(D(x) for x in a),
    "floor": lambda x: D(x).to_integral_value(rounding="ROUND_FLOOR"),
    "ceiling": lambda x: D(x).to_integral_value(rounding="ROUND_CEILING"),
}
CONSTS = {"pi": None, "E": None, "oo": Decimal("Infinity")}
RESERVED = set(FUNCS) | set(CONSTS)


# ------------------------------------------------------------------ Python 风格表达式的求值器

def _eval_node(n, env):
    if isinstance(n, ast.Expression):
        return _eval_node(n.body, env)
    if isinstance(n, ast.Constant):
        if isinstance(n.value, bool) or not isinstance(n.value, (int, float)):
            raise MathError("不支持的常量")
        return Decimal(repr(n.value)) if isinstance(n.value, float) else Decimal(n.value)
    if isinstance(n, ast.UnaryOp):
        v = _eval_node(n.operand, env)
        if isinstance(n.op, ast.USub): return -v
        if isinstance(n.op, ast.UAdd): return v
        raise MathError("不支持的一元运算")
    if isinstance(n, ast.BinOp):
        a, b = _eval_node(n.left, env), _eval_node(n.right, env)
        if isinstance(n.op, ast.Add): return a + b
        if isinstance(n.op, ast.Sub): return a - b
        if isinstance(n.op, ast.Mult): return a * b
        if isinstance(n.op, ast.Div):
            if b == 0: raise MathError("除以零")
            return a / b
        if isinstance(n.op, ast.Pow): return f_pow(a, b)
        raise MathError("不支持的运算")
    if isinstance(n, ast.Name):
        if n.id == "pi": return pi()
        if n.id == "E": return Decimal(1).exp()
        if n.id == "oo": return Decimal("Infinity")
        if n.id in env: return env[n.id]
        raise MathError(f"未知名称 {n.id}")
    if isinstance(n, ast.Call):
        if not isinstance(n.func, ast.Name) or n.func.id not in FUNCS or n.keywords:
            raise MathError("不支持的函数")
        return FUNCS[n.func.id](*[_eval_node(a, env) for a in n.args])
    raise MathError("不支持的语法")


def free_symbols(py: str) -> list[str]:
    tree = ast.parse(py, mode="eval")
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id not in RESERVED and n.id not in out:
            out.append(n.id)
    return out


def eval_py(py: str, env=None) -> Decimal:
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError as e:
        raise MathError(f"语法错误：{e}")
    with localcontext() as c:
        c.prec = PREC
        try:
            return +_eval_node(tree, env or {})
        except (InvalidOperation, ZeroDivisionError, OverflowError, RecursionError) as e:
            raise MathError(f"计算失败：{e}")


# ------------------------------------------------------------------ LaTeX → Python 表达式字符串

_TOKEN = re.compile(r"\s*(?:(\d+\.\d+|\d+|\.\d+)|(\\[A-Za-z]+|\\.)|([A-Za-z])|(.))", re.S)

_FUNC_CMDS = {"sin": "sin", "cos": "cos", "tan": "tan", "arcsin": "asin", "arccos": "acos", "arctan": "atan",
              "ln": "ln", "exp": "exp", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh", "log": "log"}
_GREEK = {"alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "theta", "lambda", "mu", "nu", "rho", "sigma",
          "tau", "phi", "varphi", "omega", "kappa", "eta", "zeta", "xi", "psi", "chi"}
_SPACING = {"\\,", "\\;", "\\:", "\\!", "\\ ", "\\quad", "\\qquad", "\\displaystyle", "\\left", "\\right", "\\bigl",
            "\\bigr", "\\Big", "\\big"}


def _clean(s: str) -> str:
    s = uni_to_latex(s)
    s = s.replace("$", "")
    s = re.sub(r"\\(?:dfrac|tfrac)", r"\\frac", s)
    s = re.sub(r"\\(?:text|mathrm|operatorname)\{\s*([A-Za-z]+)\s*\}", r"\\\1 ", s)
    s = re.sub(r"\\boxed\{(.*)\}\s*$", r"\1", s.strip(), flags=re.S)
    s = s.replace("\\left", "").replace("\\right", "").replace("\\displaystyle", "")
    for t in ("\\,", "\\;", "\\:", "\\!", "\\ ", "\\quad", "\\qquad"):
        s = s.replace(t, " ")
    s = s.replace("\\lbrace", "\\{").replace("\\rbrace", "\\}")
    return s.strip().rstrip(".。;；").strip()


def tokenize(s: str):
    toks, i = [], 0
    while i < len(s):
        m = _TOKEN.match(s, i)
        if not m or m.end() == i:
            break
        i = m.end()
        if m.group(1): toks.append(("n", m.group(1)))
        elif m.group(2): toks.append(("c", m.group(2)))
        elif m.group(3): toks.append(("v", m.group(3)))
        elif m.group(4) is not None and m.group(4).strip():
            toks.append(("o", m.group(4)))
    return toks


class _P:
    """递归下降：把一段 token 解析为 Python 表达式字符串。"""

    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def eat(self, kind=None, val=None):
        k, v = self.peek()
        if k is None or (kind and k != kind) or (val is not None and v != val):
            raise MathError(f"期望 {val or kind}，实际 {v}")
        self.i += 1
        return v

    def at_end(self):
        return self.i >= len(self.t)

    def starts_atom(self):
        k, v = self.peek()
        if k in ("n", "v"):
            return True
        if k == "o" and v in "({|":
            return True
        if k == "c":
            return v not in ("\\cdot", "\\times", "\\div", "\\pm", "\\mp", "\\cup", "\\cap", "\\le", "\\ge", "\\ne",
                             "\\in", "\\}", "\\rfloor", "\\rceil", "\\leq", "\\geq", "\\to", "\\neq")
        return False

    def expr(self):
        s = self.term()
        while True:
            k, v = self.peek()
            if k == "o" and v in "+-":
                self.i += 1
                r = self.term()
                s = f"({s}{v}{r})"
            else:
                return s

    def term(self):
        s = self.unary()
        while True:
            k, v = self.peek()
            if (k == "o" and v in "*/") or (k == "c" and v in ("\\cdot", "\\times", "\\div")):
                self.i += 1
                op = "/" if v in ("/", "\\div") else "*"
                s = f"({s}{op}{self.unary()})"
            elif self.starts_atom() and not (k == "o" and v == "|" and self._closing_bar()):
                s = f"({s}*{self.unary()})"
            else:
                return s

    def _closing_bar(self):
        return getattr(self, "_in_abs", 0) > 0

    def unary(self):
        k, v = self.peek()
        if k == "o" and v == "-":
            self.i += 1
            return f"(-{self.unary()})"
        if k == "o" and v == "+":
            self.i += 1
            return self.unary()
        return self.power()

    def power(self):
        b = self.postfix()
        k, v = self.peek()
        if k == "o" and v == "^":
            self.i += 1
            e = self.exponent()
            return f"({b}**{e})"
        return b

    def exponent(self):
        k, v = self.peek()
        if k == "o" and v == "-":
            self.i += 1
            return f"(-{self.exponent()})"
        if k == "o" and v == "{":
            self.i += 1
            e = self.expr()
            self.eat("o", "}")
            return f"({e})"
        if k == "n":
            # LaTeX 里 x^23 是 x^2 乘 3：不带花括号的指数只取一个数字
            if len(v) > 1 and "." not in v:
                self.t[self.i] = ("n", v[1:])
                return v[0]
            self.i += 1
            return v
        return self.atom()

    def postfix(self):
        a = self.atom()
        while True:
            k, v = self.peek()
            if k == "o" and v == "!":
                self.i += 1
                if self.peek() == ("o", "!"):
                    self.i += 1
                    a = f"dfactorial({a})"
                else:
                    a = f"factorial({a})"
            elif k == "o" and v == "_":
                raise MathError("意外的下标")
            else:
                return a

    def group_arg(self):
        k, v = self.peek()
        if k == "o" and v == "{":
            self.i += 1
            e = self.expr()
            self.eat("o", "}")
            return e
        if k == "n" and len(v) > 1 and "." not in v:  # \frac12 ＝ 1/2：不带花括号的参数只取一个数字
            self.t[self.i] = ("n", v[1:])
            return v[0]
        return self.atom()

    def atom(self):
        k, v = self.peek()
        if k == "n":
            self.i += 1
            return f"({v})" if v.startswith(".") else v
        if k == "v":
            self.i += 1
            if v == "e":
                return "E"
            if v == "i":
                raise MathError("虚数单位")
            sym = v
            k2, v2 = self.peek()
            if k2 == "o" and v2 == "_":
                self.i += 1
                k3, v3 = self.peek()
                if k3 == "o" and v3 == "{":
                    self.i += 1
                    parts = []
                    while self.peek() != ("o", "}"):
                        parts.append(self.peek()[1]); self.i += 1
                        if self.at_end(): raise MathError("下标未闭合")
                    self.i += 1
                    sym += "_" + "".join(re.sub(r"\W", "", p) for p in parts)
                else:
                    self.i += 1
                    sym += "_" + re.sub(r"\W", "", v3)
            return sym
        if k == "o":
            if v == "(":
                self.i += 1
                e = self.expr()
                self.eat("o", ")")
                return f"({e})"
            if v == "{":
                self.i += 1
                e = self.expr()
                self.eat("o", "}")
                return f"({e})"
            if v == "|":
                self.i += 1
                self._in_abs = getattr(self, "_in_abs", 0) + 1
                e = self.expr()
                self._in_abs -= 1
                self.eat("o", "|")
                return f"abs({e})"
            raise MathError(f"意外的符号 {v}")
        if k == "c":
            self.i += 1
            if v == "\\frac":
                a, b = self.group_arg(), self.group_arg()
                return f"(({a})/({b}))"
            if v == "\\sqrt":
                n = None
                if self.peek() == ("o", "["):
                    self.i += 1
                    n = self.expr()
                    self.eat("o", "]")
                a = self.group_arg()
                return f"root({a},{n})" if n else f"sqrt({a})"
            if v in ("\\lfloor", "\\lceil"):
                e = self.expr()
                self.eat("c", "\\rfloor" if v == "\\lfloor" else "\\rceil")
                return f"{'floor' if v == chr(92) + 'lfloor' else 'ceiling'}({e})"
            if v in ("\\max", "\\min"):
                self.eat("o", "(")
                args = [self.expr()]
                while self.peek() == ("o", ","):
                    self.i += 1
                    args.append(self.expr())
                self.eat("o", ")")
                return f"{v[1:]}({','.join(args)})"
            if v == "\\pi": return "pi"
            if v == "\\infty": return "oo"
            if v == "\\binom":
                a, b = self.group_arg(), self.group_arg()
                return f"binomial({a},{b})"
            if v[1:] in _FUNC_CMDS:
                name = _FUNC_CMDS[v[1:]]
                if name == "log" and self.peek() == ("o", "_"):
                    self.i += 1
                    b = self.group_arg()
                    a = self.func_arg()
                    return f"logb({b},{a})"
                return f"{name}({self.func_arg()})"
            if v[1:] in _GREEK:
                return "g_" + v[1:]
            raise MathError(f"不支持的命令 {v}")
        raise MathError("表达式不完整")

    def func_arg(self):
        k, v = self.peek()
        if k == "o" and v == "(":
            self.i += 1
            e = self.expr()
            self.eat("o", ")")
            return e
        # \sin x、\ln 2、\sin^2 x 不支持 ^ 在函数名后，直接拒绝
        if k == "o" and v == "^":
            raise MathError("函数名后直接带指数（如 \\sin^2 x）暂不支持")
        return self.power()


def latex_expr_to_py(tokens) -> str:
    p = _P(tokens)
    if not tokens:
        raise MathError("空表达式")
    s = p.expr()
    if not p.at_end():
        raise MathError(f"多余内容：{p.peek()[1]}")
    return s


# ------------------------------------------------------------------ 结构

def _split_top(toks, sep):
    """按顶层（括号、花括号之外）的分隔符切 token。sep: ('o', ',') 或 ('c', '\\cup')。"""
    out, cur, d = [], [], 0
    for t in toks:
        if t[0] == "o" and t[1] in "([{":
            d += 1
        elif t[0] == "o" and t[1] in ")]}":
            d -= 1
        elif t == ("c", "\\{"):
            d += 1
        elif t == ("c", "\\}"):
            d -= 1
        if d == 0 and t == sep:
            out.append(cur); cur = []
        else:
            cur.append(t)
    out.append(cur)
    return out


def _match_close(toks):
    """toks[0] 是开括号时，返回与之配平的位置（'(' '[' 视为同类，便于半开区间）。"""
    d = 0
    for i, t in enumerate(toks):
        if t[0] == "o" and t[1] in "([":
            d += 1
        elif t[0] == "o" and t[1] in ")]":
            d -= 1
            if d == 0:
                return i
        elif t == ("c", "\\{"):
            d += 1
        elif t == ("c", "\\}"):
            d -= 1
    return -1


def _parse_group(toks):
    if not toks:
        raise MathError("空")
    # \{ ... \} 集合
    if toks[0] == ("c", "\\{") and toks[-1] == ("c", "\\}"):
        inner = toks[1:-1]
        parts = _split_top(inner, ("o", ",")) if inner else []
        return ("set", [_parse_elem(p) for p in parts])
    if toks[0] == ("c", "\\emptyset"):
        return ("set", [])
    if toks[0][0] == "o" and toks[0][1] in "([":
        j = _match_close(toks)
        if j == len(toks) - 1:
            inner = toks[1:j]
            parts = _split_top(inner, ("o", ","))
            if len(parts) > 1:
                return ("seq", toks[0][1] + toks[j][1], [_parse_elem(p) for p in parts])
    parts = _split_top(toks, ("o", ","))
    if len(parts) > 1:
        return ("list", [_parse_elem(p) for p in parts])
    return _parse_elem(toks)


def _parse_elem(toks):
    # \pm：a \pm b → 集合 {a+b, a-b}
    idx = [i for i, t in enumerate(toks) if t == ("c", "\\pm")]
    if idx:
        i = idx[0]
        outs = []
        for op in ("+", "-"):
            t2 = toks[:i] + [("o", op)] + toks[i + 1:]
            outs.append(("num", latex_expr_to_py(t2)))
        return ("set", outs)
    if toks and toks[0][0] == "o" and toks[0][1] in "([":
        j = _match_close(toks)
        if j == len(toks) - 1:
            inner = _split_top(toks[1:j], ("o", ","))
            if len(inner) > 1:
                return ("seq", toks[0][1] + toks[j][1], [_parse_elem(p) for p in inner])
    return ("num", latex_expr_to_py(toks))


_PLAIN_FUNC = re.compile(r"(?<![\\A-Za-z])(sqrt|pi|inf|infinity|exp|sin|cos|tan|log|ln|arcsin|arccos|arctan|abs|factorial|binomial)(?![A-Za-z])|\*\*")


def parse_latex(s: str):
    """LaTeX 答案 → 结构节点；解析不了抛 MathError。
    节点：("num", python表达式串)、("seq", 括号, [节点])、("set", [节点])、("list", [节点])、("union", [节点])。"""
    s = _clean(s)
    if _PLAIN_FUNC.search(s):  # sqrt(2)、pi 这类程序写法在 LaTeX 里会被误读成一串变量相乘，宁可读不了也不误读
        raise MathError("答案里是程序写法（sqrt/pi/** 等），不是 LaTeX")
    # 去掉单字母变量名前缀：x = 3
    m = re.match(r"^[A-Za-z](?:_\{?\w+\}?)?\s*=\s*(?!=)", s)
    if m and "=" not in s[m.end():]:
        s = s[m.end():]
    toks = tokenize(s)
    if not toks:
        raise MathError("空答案")
    for t in toks:
        if t in (("c", "\\le"), ("c", "\\ge"), ("c", "\\ne"), ("c", "\\in"), ("o", "="), ("o", "<"), ("o", ">")):
            raise MathError("含不等式或等式，暂不自动解析")
    parts = _split_top(toks, ("c", "\\cup"))
    if len(parts) > 1:
        return ("union", [_parse_group(p) for p in parts])
    return _parse_group(toks)


def parse_py(s: str):
    """生产者另交的"sympy 写法"→ 结构节点。支持标量表达式、(a,b,c) 元组、{a,b} 集合。"""
    s = (s or "").strip()
    if not s:
        raise MathError("空")
    if "^" in s:
        raise MathError("sympy 写法里请用 ** 表示乘方")
    try:
        tree = ast.parse(s, mode="eval").body
    except SyntaxError as e:
        raise MathError(f"语法错误：{e}")

    def conv(n):
        if isinstance(n, ast.Tuple):
            return ("seq", "()", [conv(x) for x in n.elts])
        if isinstance(n, ast.List):
            return ("seq", "[]", [conv(x) for x in n.elts])
        if isinstance(n, ast.Set):
            return ("set", [conv(x) for x in n.elts])
        return ("num", ast.unparse(n))
    return conv(tree)


# ------------------------------------------------------------------ 数值与结构比较

def _points(names, k=4):
    rnd = random.Random(20260502)
    pts = []
    for j in range(k):
        pts.append({n: Decimal(rnd.randint(13, 47)) / Decimal(rnd.randint(7, 11)) for n in names})
    for j in range(k):  # 整数点：阶乘、二项式系数、取整、负数底的整数次幂只在整数点有定义
        pts.append({n: Decimal(rnd.randint(4, 14)) for n in names})
    return pts


def _tol_equal(a: Decimal, b: Decimal) -> bool:
    if a.is_infinite() or b.is_infinite():
        return a == b
    scale = max(Decimal(1), abs(a), abs(b))
    return abs(a - b) <= scale * (Decimal(10) ** -TOL_DIGITS)


def num_value(py: str):
    """无变量表达式的数值（Decimal）；有变量返回 None；算不了抛 MathError。"""
    if free_symbols(py):
        return None
    return eval_py(py)


def num_equal(pa: str, pb: str):
    """返回 True/False；算不出来（含变量取点都失败）抛 MathError。含变量：在若干随机点上代入比较。"""
    names = sorted(set(free_symbols(pa)) | set(free_symbols(pb)))
    if not names:
        return _tol_equal(eval_py(pa), eval_py(pb))
    ok = 0
    for env in _points(names, 5):
        try:
            a, b = eval_py(pa, env), eval_py(pb, env)
        except MathError:
            continue
        if not _tol_equal(a, b):
            return False
        ok += 1
    if ok < 3:
        raise MathError("变量代入后可算的点太少，无法判断")
    return True


def struct_equal(a, b):
    """True/False；结构种类不同且无法比较时抛 MathError。"""
    ka, kb = a[0], b[0]
    if ka == "num" and kb == "num":
        return num_equal(a[1], b[1])
    if ka == "seq" and kb == "seq":
        if a[1] != b[1]:
            return False
        return len(a[2]) == len(b[2]) and all(struct_equal(x, y) for x, y in zip(a[2], b[2]))
    if ka in ("set", "union", "list") and kb == ka:
        xs, ys = a[1], b[1]
        if len(xs) != len(ys):
            return False
        left = list(ys)
        for x in xs:
            for j, y in enumerate(left):
                try:
                    if struct_equal(x, y):
                        del left[j]
                        break
                except MathError:
                    continue
            else:
                return False
        return True
    raise MathError("结构种类不同，无法自动比较")


def can_parse(s: str) -> tuple[bool, str]:
    """能否被解析并求值（用于覆盖率统计和格式校验）。"""
    try:
        node = parse_latex(s)
        _check_eval(node)
        return True, node[0]
    except MathError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001  —— 任何意外都算解析不了，绝不让它冒出去
        return False, f"{type(e).__name__}: {e}"


def _check_eval(node):
    k = node[0]
    if k == "num":
        names = free_symbols(node[1])
        if names:
            for env in _points(names, 4):
                try:
                    eval_py(node[1], env)
                    return
                except MathError:
                    continue
            raise MathError("代入后算不出")
        eval_py(node[1])
    else:
        for x in (node[2] if k == "seq" else node[1]):
            _check_eval(x)
