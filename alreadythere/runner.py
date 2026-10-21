"""代码运行器（2.2，A 档）。

隔离级别：A 档 —— 这【不是】安全隔离。它只做下面几件事：
1. 每段代码在单独的子进程里运行（用本机的 Python，所以能用你装好的 numpy、sympy、mpmath 等）；
2. 有超时，超时就把整个子进程树杀掉；
3. 输出长度有上限，多出的部分截掉；
4. 在一个临时空目录里运行，运行完删除；
5. 子进程拿不到你的密钥：环境变量只保留运行 Python 所必需的几项（PATH、SYSTEMROOT 等），
   其他一律不传，所以读不到 密钥.env 里的内容（那是程序读了文件才放进自己内存的，不在环境变量里）。
6. Linux/macOS 上额外限制内存上限（Windows 上做不到，不限制）。
它【不会】阻止模型写的代码读写你硬盘上的其他文件、联网、或占满 CPU。
因此默认关闭，只有你在参数面板勾选「允许运行模型写的代码」才会运行。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .util import sha256_text

ENV_KEEP = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
            "LANG", "LC_ALL", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "LD_LIBRARY_PATH")
PROBE_LIBS = ("numpy", "scipy", "sympy", "mpmath")
MEM_LIMIT_BYTES = 4 * 1024 ** 3


def _python():
    """用哪个 Python 运行代码：平时就是当前这个；打包成 exe 后 sys.executable 是程序自己，要另找系统里的 python。"""
    if getattr(sys, "frozen", False):
        return shutil.which("python") or shutil.which("python3") or ""
    return sys.executable


def clean_env():
    env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


def _preexec():  # 仅 POSIX
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT_BYTES, MEM_LIMIT_BYTES))
    except Exception:  # noqa: BLE001
        pass
    os.setsid()


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=0x08000000)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


_RND = re.compile(r"\b(random|numpy\.random|np\.random|secrets)\b|\bimport\s+random\b|\bfrom\s+random\b")
_SEED = re.compile(r"\bseed\s*\(\s*([^)]*?)\s*\)|default_rng\s*\(\s*([^)]+?)\s*\)|RandomState\s*\(\s*([^)]+?)\s*\)|\bRandom\s*\(\s*([^)]+?)\s*\)")


def env_info(code):
    """规格 9.3：运行环境与随机数记录。程序不替代码设随机种子（那会改变代码的行为），
    只记下：Python 版本与平台、PYTHONHASHSEED（固定为 0）、代码里有没有用随机数、声明了什么种子。"""
    uses = bool(_RND.search(code or ""))
    seeds = [next(g for g in m.groups() if g) for m in _SEED.finditer(code or "") if any(m.groups())]
    return {"python": platform.python_version(), "platform": platform.platform(), "hashseed": "0",
            "uses_random": uses, "declared_seed": seeds[0] if seeds else None,
            "seed_note": ("代码用了随机数但没有声明种子，结果可能每次不同" if uses and not seeds else
                          ("代码声明的种子：" + seeds[0] if seeds else "代码没有用随机数"))}


def run_code(code, timeout=60, max_out=6000):
    """运行一段 Python 代码。返回 {status, stdout, stderr, seconds, code_hash, truncated}。
    status：完成 / 出错（退出码非 0）/ 超时 / 无法启动。不抛异常。"""
    code = code or ""
    h = sha256_text(code)
    work = tempfile.mkdtemp(prefix="gz_run_")
    t0 = time.time()
    try:
        script = os.path.join(work, "main.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # 不弹黑窗
        else:
            kw["preexec_fn"] = _preexec
        try:
            py = _python()
            if not py:
                return {"status": "无法启动", "stdout": "", "stderr": "找不到 Python", "seconds": 0.0, "code_hash": h,
                        "truncated": False}
            proc = subprocess.Popen([py, "-I", "-X", "utf8", script], cwd=work, env=clean_env(),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        except OSError as e:
            return {"status": "无法启动", "stdout": "", "stderr": str(e), "seconds": 0.0, "code_hash": h,
                    "truncated": False}
        status = "完成"
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                out, err = b"", b""
            status = "超时"
        if status == "完成" and proc.returncode != 0:
            status = "出错"
        so, se = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        trunc = len(so) > max_out or len(se) > max_out
        return {"status": status, "stdout": so[:max_out], "stderr": se[-max_out:] if len(se) > max_out else se,
                "seconds": round(time.time() - t0, 2), "code_hash": h, "truncated": trunc, "env": env_info(code)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def probe_libs():
    """检查本机 Python 里装了哪些常用数学库（不装、不联网）。返回 {库名: 版本或 None}。"""
    code = ("import importlib\nfor n in %r:\n  try:\n    m=importlib.import_module(n);print(n,getattr(m,'__version__','?'))\n"
            "  except Exception:\n    print(n,'-')\n" % (PROBE_LIBS,))
    r = run_code(code, timeout=60)
    out = {n: None for n in PROBE_LIBS}
    for line in r["stdout"].splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in out and parts[1] != "-":
            out[parts[0]] = parts[1]
    return out
