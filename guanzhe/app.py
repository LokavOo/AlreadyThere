"""窗口版（1.3）：本机小服务 + 应用窗口（Edge 应用模式，没有地址栏）。

只监听 127.0.0.1，别的电脑访问不到。密钥只在本机读取，页面上只显示"已填/未填"，不显示内容。
运行、连通测试都在独立的子进程里进行，输出写进项目文件夹里的"运行日志.txt"；
关掉窗口不会打断正在进行的运行，重新打开窗口可以接着看。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import feed
from .config import load_secrets, load_settings, memory_of

SETTINGS = "设置.json"
SECRETS = "密钥.env"
LOCK = "运行中.txt"
LOG = "运行日志.txt"
SAFE = re.compile(r"^[^\\/:*?\"<>|]{1,60}$")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _py():
    """子进程用带控制台输出的 python.exe（pythonw 打印不了东西）。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


FROZEN = bool(getattr(sys, "frozen", False))  # 1.9：打包成 exe 后运行


def _cmd():
    """启动子进程（运行、连通测试、新建项目、详细记录）的命令前半段：
    平时是 python -m guanzhe；打包成 exe 后直接调用 exe 自己。"""
    return [sys.executable] if FROZEN else [_py(), "-m", "guanzhe"]


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class App:
    def __init__(self, base: Path, prog: Path | None = None, moved=None):
        self.base = Path(base)  # 1.9：数据文件夹（设置、密钥、题目、项目）
        self.prog = Path(prog) if prog else PROG  # 程序文件夹（examples 在这里）
        self.moved = moved or []
        self.ping_lines: list[str] = []
        self.ping_running = False
        self.last_beat = time.time()

    # ---------- 状态 ----------
    def running_pid(self, proj: Path) -> int:
        f = proj / LOCK
        if f.exists():
            try:
                pid = int(f.read_text(encoding="utf-8").split()[0])
            except (ValueError, IndexError):
                pid = 0
            if _alive(pid):
                return pid
            f.unlink(missing_ok=True)
        return 0

    def projects(self):
        out = []
        for d in sorted(self.base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir() and (d / "project.db").exists():
                done = (d / "outputs" / "结论.md").exists()
                out.append({"name": d.name, "running": bool(self.running_pid(d)), "done": done})
        return out

    def questions(self):
        out = []
        for f in sorted(self.base.glob("*.json")):
            if f.name == SETTINGS:
                continue
            try:
                obj = json.loads(f.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(obj, dict) and "items" in obj and "question" in obj:
                out.append({"file": f.name, "question": obj["question"], "count": len(obj["items"]),
                            "trusted": bool(obj.get("trusted_sites"))})
        ex = self.prog / "examples"  # 1.9：examples 里的题目直接列出，不用复制
        have = {x["file"] for x in out}
        for f in sorted(ex.glob("*.json")) if ex.exists() else []:
            if f.name == SETTINGS or f.name in have:
                continue
            try:
                obj = json.loads(f.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(obj, dict) and "items" in obj and "question" in obj:
                out.append({"file": "examples/" + f.name, "question": obj["question"] + "（示例题目）",
                            "count": len(obj["items"]), "trusted": bool(obj.get("trusted_sites"))})
        return out

    def env(self):
        """子进程的环境：数据文件夹里运行，也要能找到程序。"""
        if FROZEN:  # 1.9.1：exe 启动自己时，让打包工具从头准备运行环境，不沿用上一个进程的临时文件夹
            return dict(os.environ, PYTHONIOENCODING="utf-8", PYINSTALLER_RESET_ENVIRONMENT="1")
        pp = os.environ.get("PYTHONPATH")
        return dict(os.environ, PYTHONIOENCODING="utf-8",
                    PYTHONPATH=str(self.prog) + (os.pathsep + pp if pp else ""))

    def question_path(self, qfile):
        if qfile.startswith("examples/"):
            return self.prog / "examples" / Path(qfile).name
        return self.base / qfile

    def state(self):
        roles, err = [], None
        try:
            st = load_settings(self.base / SETTINGS)
            sec = load_secrets(self.base / SECRETS)
            for r, s in st["roles"].items():
                try:
                    mem = memory_of(s)
                except ValueError:
                    mem = {"mode": "全局" if s.get("kind") == "审查者" else "固定", "see": ["self"], "rounds": 1,
                           "bad": True}
                roles.append({"role": r, "name": s.get("series", r), "model": s.get("model", ""),
                              "kind": s.get("kind", ""), "key": bool(sec.get(s.get("key_name", ""))),
                              "memory": {"mode": mem["mode"], "see": mem.get("see", ["self"]),
                                         "rounds": mem.get("rounds", 1), "bad": mem.get("bad", False)}})
            max_rounds = int(st["params"].get("max_rounds", 6))
            sk = st.get("search", {}).get("key_name", "TAVILY_API_KEY")
            search_key = bool(sec.get(sk))
        except FileNotFoundError as e:
            err, search_key, max_rounds = f"找不到文件：{Path(str(e.filename)).name}", False, 6
        except ValueError as e:
            err, search_key, max_rounds = f"设置文件格式有误：{e}", False, 6
        return {"data_dir": str(self.base), "moved": self.moved, "roles": roles, "max_rounds": max_rounds, "search_key": search_key, "secrets_file": (self.base / SECRETS).exists(),
                "error": err, "projects": self.projects(), "questions": self.questions(),
                "ping": {"running": self.ping_running, "lines": self.ping_lines[-40:]}}

    # ---------- 动作 ----------
    def ping(self):
        if self.ping_running:
            return {"ok": False, "msg": "连通测试正在进行"}
        self.ping_running, self.ping_lines = True, ["开始连通测试……"]

        def work():
            try:
                p = subprocess.Popen([*_cmd(), "ping", "--settings", SETTINGS, "--secrets", SECRETS],
                                     cwd=self.base, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     env=self.env(), creationflags=NOWIN)
                for raw in p.stdout:
                    self.ping_lines.append(raw.decode("utf-8", "replace").rstrip())
                p.wait()
                self.ping_lines.append("连通测试结束。")
            except OSError as e:
                self.ping_lines.append(f"没能启动连通测试：{e}")
            finally:
                self.ping_running = False
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def new_project(self, name: str, qfile: str):
        if not SAFE.match(name or "") or name.startswith("."):
            return {"ok": False, "msg": "项目名不能为空，也不能含 \\ / : * ? \" < > | 这些符号"}
        if (self.base / name / "project.db").exists():
            return {"ok": False, "msg": f"已经有叫「{name}」的项目了，换个名字"}
        if qfile == "__demo__":
            args = ["demo", name]
        else:
            if not SAFE.match(Path(qfile).name) or not self.question_path(qfile).exists():
                return {"ok": False, "msg": "找不到这个题目文件"}
            args = ["new", name, "--project", str(self.question_path(qfile))]
        r = subprocess.run([*_cmd(), *args], cwd=self.base, capture_output=True,
                           env=self.env(), creationflags=NOWIN)
        out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        if r.returncode != 0:
            return {"ok": False, "msg": out[-400:] or "创建失败"}
        return {"ok": True, "name": name}

    def run(self, name: str):
        proj = self.base / name
        if not (proj / "project.db").exists():
            return {"ok": False, "msg": "找不到这个项目"}
        if self.running_pid(proj):
            return {"ok": False, "msg": "这个项目已经在运行了"}
        log = open(proj / LOG, "ab")
        log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 开始运行 ====\n".encode("utf-8"))
        log.flush()
        p = subprocess.Popen([*_cmd(), "run", name, "--settings", SETTINGS, "--secrets", SECRETS],
                             cwd=self.base, stdout=log, stderr=subprocess.STDOUT,
                             env=self.env(), creationflags=NOWIN)
        (proj / LOCK).write_text(f"{p.pid}\n", encoding="utf-8")

        def reap():
            p.wait()
            log.close()
            (proj / LOCK).unlink(missing_ok=True)
        threading.Thread(target=reap, daemon=True).start()
        return {"ok": True}

    def trusted_ok(self, name: str) -> bool:
        """1.5：项目的问题里（或设置里）有没有可信网站名单；没有的话独立核验不会启动。"""
        try:
            cfg = json.loads((self.base / name / "config.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            cfg = {}
        if cfg.get("trusted_sites"):
            return True
        try:
            return bool(json.loads((self.base / SETTINGS).read_text(encoding="utf-8-sig")).get("trusted_sites"))
        except (OSError, ValueError):
            return False

    def set_memory(self, roles: dict):
        """1.5：在窗口里选记忆模式，由程序写回 设置.json（只改各角色的 memory，其他字段原样保留；先备份成 设置.json.bak）。"""
        path = self.base / SETTINGS
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"ok": False, "msg": "找不到 设置.json"}
        except ValueError as e:
            return {"ok": False, "msg": f"设置.json 格式有误，没有改动：{e}"}
        all_roles = obj.get("roles") or {}
        producers = [r for r, v in all_roles.items() if v.get("kind") == "生产者"]
        max_rounds = int((obj.get("params") or {}).get("max_rounds", 6))
        if not isinstance(roles, dict) or not roles:
            return {"ok": False, "msg": "没有收到要保存的内容"}
        new = {}
        for r, m in roles.items():
            if r not in all_roles or not isinstance(m, dict):
                return {"ok": False, "msg": f"设置里没有角色 {r}"}
            mode = m.get("mode")
            kind = all_roles[r].get("kind")
            allowed = ("全局", "固定", "全新") if kind == "生产者" else ("全局", "全新")
            if mode not in allowed:
                return {"ok": False, "msg": f"{r} 的记忆模式只能是 {' / '.join(allowed)}"}
            if mode == "固定":
                see = [x for x in (m.get("see") or []) if x == "self" or (x in producers and x != r)]
                if not see:
                    return {"ok": False, "msg": f"{r} 选了「固定」，至少要勾一个能看的角色"}
                try:
                    n = int(m.get("rounds", 1))
                except (TypeError, ValueError):
                    n = 0
                if not 1 <= n <= max_rounds:
                    return {"ok": False, "msg": f"{r} 看几轮只能填 1 到 {max_rounds}"}
                new[r] = {"mode": "固定", "see": list(dict.fromkeys(see)), "rounds": n}
            else:
                new[r] = {"mode": mode}
        if path.exists():
            shutil.copy2(path, path.with_name(SETTINGS + ".bak"))
        for r, m in new.items():
            obj["roles"][r]["memory"] = m
        tmp = path.with_name(SETTINGS + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        running = any(x["running"] for x in self.projects())
        return {"ok": True, "msg": "已保存到 设置.json（改之前的版本备份为 设置.json.bak）。" +
                ("正在运行的项目不受影响，下次运行时生效。" if running else "下次运行时生效。")}

    def log_tail(self, name: str):
        f = self.base / name / LOG
        if not f.exists():
            return ""
        return f.read_bytes()[-3000:].decode("utf-8", "replace")

    def conclusion(self, name: str):
        f = self.base / name / "outputs" / "结论.md"
        return f.read_text(encoding="utf-8-sig") if f.exists() else None

    def detail(self, name: str):
        port = 8800 + (abs(hash(name)) % 150)
        subprocess.Popen([*_cmd(), "serve", name, "--port", str(port)], cwd=self.base,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env(), creationflags=NOWIN)
        return {"ok": True, "url": f"http://127.0.0.1:{port}/"}


def make_handler(app: App):
    page = (Path(__file__).parent / "app_page.html").read_text(encoding="utf-8")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="application/json; charset=utf-8", code=200):
            data = body if isinstance(body, bytes) else (
                body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8"))
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _proj(self, q):
            name = (q.get("p") or [""])[0]
            if not SAFE.match(name) or not (app.base / name / "project.db").exists():
                return None
            return name

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            app.last_beat = time.time()
            if u.path == "/":
                return self._send(page, "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._send(app.state())
            if u.path == "/api/feed":
                name = self._proj(q)
                if not name:
                    return self._send({"messages": [], "last": 0, "running": False})
                after = int((q.get("after") or ["0"])[0] or 0)
                r = feed.messages(app.base / name, after)
                r["running"] = bool(app.running_pid(app.base / name))
                r["log"] = app.log_tail(name) if not r["running"] else ""
                r["trusted"] = app.trusted_ok(name)
                try:
                    r["question"] = json.loads((app.base / name / "config.json").read_text(encoding="utf-8-sig"))["question"]
                except (OSError, ValueError, KeyError):
                    r["question"] = ""
                return self._send(r)
            if u.path == "/api/conclusion":
                name = self._proj(q)
                return self._send({"text": app.conclusion(name) if name else None})
            self._send({"error": "not found"}, code=404)

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if self.headers.get("X-Guanzhe") != "1":  # 只接受本窗口发来的请求
                return self._send({"ok": False, "msg": "拒绝"}, code=403)
            if u.path == "/api/ping":
                return self._send(app.ping())
            if u.path == "/api/new":
                return self._send(app.new_project(str(body.get("name", "")).strip(), str(body.get("question", ""))))
            if u.path == "/api/run":
                return self._send(app.run(str(body.get("name", ""))))
            if u.path == "/api/memory":
                return self._send(app.set_memory(body.get("roles")))
            if u.path == "/api/detail":
                return self._send(app.detail(str(body.get("name", ""))))
            self._send({"error": "not found"}, code=404)
    return H


def open_window(url: str):
    if os.name == "nt":
        for exe in [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                    os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe")]:
            if Path(exe).exists():
                subprocess.Popen([exe, f"--app={url}", "--window-size=1180,820"], creationflags=NOWIN)
                return
    webbrowser.open(url)


PROG = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[1]
DATA_NAME = "数据"


def _is_program(d: Path) -> bool:
    return (d / "guanzhe" / "__init__.py").exists() or (d / "AlreadyThere.exe").exists()


def _version_key(d: Path):
    import re as _re
    m = _re.search(r"_v(\d+)\.(\d+)", d.name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def migrate(prog: Path, data: Path):
    """1.9：第一次用固定数据文件夹时，把程序文件夹（以及旁边其他版本的程序文件夹）里的设置、密钥、题目、项目
    复制过来。只复制、不删除，数据文件夹里已有的不覆盖。返回搬过来的清单。"""
    import shutil
    data.mkdir(parents=True, exist_ok=True)
    sources = [prog] + sorted((d for d in prog.parent.iterdir()
                               if d.is_dir() and d != prog and d != data and _is_program(d)),
                              key=_version_key, reverse=True)
    moved = []
    for fname in (SETTINGS, SECRETS):
        if not (data / fname).exists():
            src = next((d / fname for d in sources if (d / fname).exists()), None)
            if src:
                shutil.copy2(src, data / fname)
                moved.append(f"{fname}（来自 {src.parent.name}）")
    for d in sources:
        for f in d.glob("*.json"):
            if f.name != SETTINGS and not (data / f.name).exists():
                try:
                    obj = json.loads(f.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    continue
                if isinstance(obj, dict) and "items" in obj and "question" in obj:
                    shutil.copy2(f, data / f.name)
                    moved.append(f"题目 {f.name}（来自 {d.name}）")
        for sub in d.iterdir():
            if sub.is_dir() and (sub / "project.db").exists() and not (data / sub.name).exists():
                shutil.copytree(sub, data / sub.name, ignore=shutil.ignore_patterns(LOCK))
                moved.append(f"项目 {sub.name}（来自 {d.name}）")
    if not (data / SETTINGS).exists() and (prog / "examples" / SETTINGS).exists():
        shutil.copy2(prog / "examples" / SETTINGS, data / SETTINGS)
        moved.append("设置.json（用 examples 里的模板，模型名等请按需修改）")
    if not (data / SECRETS).exists() and (prog / "examples" / "密钥.env.模板").exists() \
            and not (data / "密钥.env.模板").exists():
        shutil.copy2(prog / "examples" / "密钥.env.模板", data / "密钥.env.模板")
    if moved:
        with open(data / "搬迁记录.txt", "a", encoding="utf-8") as fh:
            fh.write(f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} 从程序文件夹复制到数据文件夹（原处未删除）====\n")
            fh.write("\n".join(moved) + "\n")
    return moved


def main(base=None, port=0, open_it=True, idle_exit=True):
    if base is None:  # 1.9：默认用程序文件夹旁边的固定数据文件夹
        data = PROG.parent / DATA_NAME
        moved = migrate(PROG, data)
        app = App(data, PROG, moved)
    else:
        app = App(Path(base).resolve())
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"观者自在 窗口地址：{url}")
    if open_it:
        open_window(url)
    if idle_exit:  # 窗口关掉 60 秒后自动退出（正在进行的运行不受影响）
        def watch():
            while True:
                time.sleep(10)
                if time.time() - app.last_beat > 60:
                    srv.shutdown()
                    return
        threading.Thread(target=watch, daemon=True).start()
    srv.serve_forever()
