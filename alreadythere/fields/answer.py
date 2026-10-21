"""最终答案字段（2.1）：只做基础规范化后的比较，不依赖 SymPy。

比较结果只有三种：
- 自动一致：规范化后文字相同，或两边都是数（整数、小数、分数）且数值相等；
- 不一致：两边都是数，且数值不同（这种情况可以放心判定）；
- 待人工判断：其余情况（表达式、集合、区间、文字等，没有符号计算工具时无法断定是否等价）。
程序宁可把拿不准的交给人，也不替人下"等价"或"不等价"的结论。
"""

from __future__ import annotations

import re
from decimal import Decimal
from fractions import Fraction

from . import mathcheck as MC

_FULL = str.maketrans("０１２３４５６７８９．，（）－＋＝／", "0123456789.,()-+=/")
_NUM = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)$")
_FRAC = re.compile(r"^([+-]?)\\?d?frac\{(\d+)\}\{(\d+)\}$|^([+-]?\d+)/(\d+)$")


def _strip_wrapper(s: str, name: str) -> str:
    """去掉 \\boxed{...} 这类单层外壳（花括号配平）。"""
    i = s.find(name + "{")
    while i != -1:
        j, depth = i + len(name) + 1, 1
        k = j
        while k < len(s) and depth:
            depth += {"{": 1, "}": -1}.get(s[k], 0)
            k += 1
        if depth:
            break
        s = s[:i] + s[j:k - 1] + s[k:]
        i = s.find(name + "{")
    return s


def normalize(ans) -> str:
    s = MC.uni_to_latex(str(ans if ans is not None else "").translate(_FULL).strip())  # 2.3：5√2 → 5\\sqrt{2}
    for name in ("\\boxed", "\\text", "\\mathrm", "\\textbf"):
        s = _strip_wrapper(s, name)
    s = s.replace("\\left", "").replace("\\right", "").replace("\\!", "").replace("\\,", "").replace("\\ ", "")
    s = s.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    s = s.replace("$", "").replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"^(答案[:：]?|最终答案[:：]?|answer[:：]?)", "", s, flags=re.I)
    s = s.rstrip(".。;；")
    m = re.fullmatch(r"([+-]?)\\frac\{(\d+)\}\{(\d+)\}", s)
    if m:
        s = f"{m.group(1)}{m.group(2)}/{m.group(3)}"
    return s


def as_number(s: str):
    """规范化后的字符串若是整数、小数或 a/b 分数，返回 Fraction，否则 None。"""
    try:
        if _NUM.match(s):
            return Fraction(s)
        m = re.fullmatch(r"([+-]?\d+)/(\d+)", s)
        if m and int(m.group(2)) != 0:
            return Fraction(int(m.group(1)), int(m.group(2)))
    except (ValueError, ZeroDivisionError):
        pass
    return None


def _strip_var(s: str) -> str:
    """只在另一边没有"变量名="时才去掉这一边的 x= 前缀（x=3 与 3 一致，x=3 与 y=3 不算）。"""
    return re.sub(r"^[a-zA-Z]=", "", s)


def _dec_places(norm: str):
    """规范化后是带小数点的小数字面量 → 小数位数，否则 None。"""
    m = re.fullmatch(r"[+-]?(\d*)\.(\d+)", norm)
    return len(m.group(2)) if m else None


def _is_rational_exact(py: str):
    """一个无变量表达式如果只含整数、四则运算与整数次幂，则返回 Fraction，否则 None（说明可能是无理数）。"""
    import ast
    try:
        t = ast.parse(py, mode="eval").body
    except SyntaxError:
        return None

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return Fraction(n.value)
        if isinstance(n, ast.Constant) and isinstance(n.value, float):
            return Fraction(repr(n.value))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return None if v is None else (-v if isinstance(n.op, ast.USub) else v)
        if isinstance(n, ast.BinOp):
            x, y = ev(n.left), ev(n.right)
            if x is None or y is None:
                return None
            if isinstance(n.op, ast.Add): return x + y
            if isinstance(n.op, ast.Sub): return x - y
            if isinstance(n.op, ast.Mult): return x * y
            if isinstance(n.op, ast.Div): return x / y if y != 0 else None
            if isinstance(n.op, ast.Pow) and y.denominator == 1 and abs(y) <= 1000 and not (x == 0 and y < 0):
                return x ** int(y)
        return None
    return ev(t)


def _to_node(raw, py_expr=None):
    """答案文本 → 结构节点；LaTeX 解析不了时退回生产者给的 sympy 写法；都不行返回 None。"""
    try:
        return MC.parse_latex(str(raw))
    except MC.MathError:
        pass
    if py_expr:
        try:
            return MC.parse_py(py_expr)
        except MC.MathError:
            pass
    return None


def compare_detail(a, b, a_py=None, b_py=None, b_is_gold=False) -> dict:
    """返回 {"result": 自动一致/不一致/待人工判断, "how": 判定方式, "layer": 错误层（仅小数近似判错时）}。
    how 取值：文字一致 / 数值相同 / 数值验证相等 / 数值验证不等 / 结构比较 / 小数近似≠精确值 / 按标准答案精度 / 无法解析。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return {"result": "待人工判断", "how": "无法解析"}
    if na == nb:
        return {"result": "自动一致", "how": "文字一致"}
    pa, pb = re.match(r"^[a-zA-Z]=", na), re.match(r"^[a-zA-Z]=", nb)
    if bool(pa) != bool(pb):
        na, nb = _strip_var(na), _strip_var(nb)
        if na == nb:
            return {"result": "自动一致", "how": "文字一致"}
    if pa and pb and na[0] != nb[0]:  # 变量名不同（x=3 与 y=3）：可能改变含义，不抹平
        return {"result": "待人工判断", "how": "无法解析"}
    xa, xb = as_number(na), as_number(nb)
    if xa is not None and xb is not None:
        if xa == xb:
            return {"result": "自动一致", "how": "数值相同"}
        # 两边都是数但不等：若两边都是小数字面量且位数不同，按"标准答案精度"这一例外处理在下面；这里直接判不一致
        da, db = _dec_places(na), _dec_places(nb)
        if b_is_gold and db is not None and (da is None or da > db) and round(xa, db) == xb:
            return {"result": "自动一致", "how": "按标准答案精度"}
        return {"result": "不一致", "how": "数值验证不等"}
    A, B = _to_node(a, a_py), _to_node(b, b_py)
    if A is None or B is None:
        return {"result": "待人工判断", "how": "无法解析"}
    # 小数近似 vs 精确闭式
    if A[0] == "num" and B[0] == "num" and not MC.free_symbols(A[1]) and not MC.free_symbols(B[1]):
        da, db = _dec_places(na), _dec_places(nb)
        res = _decimal_vs_exact(na, nb, da, db, A, B, b_is_gold)
        if res:
            return res
    try:
        eq = MC.struct_equal(A, B)
    except MC.MathError:
        return {"result": "待人工判断", "how": "无法解析"}
    how = "数值验证相等" if eq else "数值验证不等"
    if A[0] != "num" or B[0] != "num":
        how = "结构比较"
    return {"result": "自动一致" if eq else "不一致", "how": how}


def _decimal_vs_exact(na, nb, da, db, A, B, b_is_gold):
    """一边是小数字面量、另一边是精确闭式（无理数等）：默认判错（表示层；题目要求给近似时才算对，
    这一点程序不知道，所以交给"约束层"由评审提出）。例外：标准答案本身是小数 → 按标准答案精度比。"""
    if (da is None) == (db is None):
        # 两边都是小数，或都不是：不在这里处理（都是小数时，已在 as_number 阶段按 Fraction 比过）
        return None
    dec_side, ex_side = (A, B) if da is not None else (B, A)
    places = da if da is not None else db
    dec_is_gold = db is not None and b_is_gold  # 小数那一边是标准答案
    try:
        vd = MC.num_value(dec_side[1])
        ve = MC.num_value(ex_side[1])
    except MC.MathError:
        return None
    if vd is None or ve is None:
        return None
    exact_rat = _is_rational_exact(ex_side[1])
    if exact_rat is not None:
        # 精确是有理数：有限小数与其相等已在 as_number 阶段判过（如 0.5 与 1/2）；到这里说明不等或是循环小数
        if Fraction(str(vd)) == exact_rat:
            return {"result": "自动一致", "how": "数值相同"}
    if dec_is_gold:
        q = Decimal(1).scaleb(-places)
        if ve.quantize(q) == vd.quantize(q):
            return {"result": "自动一致", "how": "按标准答案精度"}
        return {"result": "不一致", "how": "数值验证不等"}
    close = abs(vd - ve) <= Decimal(10) ** -(max(places - 1, 0)) * 1
    if close:
        return {"result": "不一致", "how": "小数近似≠精确值", "layer": "表示层"}
    return {"result": "不一致", "how": "数值验证不等"}


def compare(a, b, a_py=None, b_py=None, b_is_gold=False) -> str:
    """宁可交给人，也不误判一致：大小写、变量名、度数符号、千分位逗号这类可能改变含义的差别都不抹平。"""
    return compare_detail(a, b, a_py, b_py, b_is_gold)["result"]


class AnswerField:
    """与 RecordField 同级的字段核对器（最终答案只有一个字段：答案）。"""
    noun = "最终答案"

    def check(self, answer) -> tuple[bool, str]:
        if not normalize(answer):
            return False, "答案为空"
        return True, ""
