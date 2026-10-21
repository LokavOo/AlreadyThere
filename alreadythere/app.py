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
from . import interject as I
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
    平时是 python -m alreadythere；打包成 exe 后直接调用 exe 自己。"""
    return [sys.executable] if FROZEN else [_py(), "-m", "alreadythere"]


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



def _effort_ok(spec):
    """哪些接口支持用 reasoning_effort 调思考强度（DeepSeek、Kimi、火山方舟；2.6 加智谱、腾讯 TokenHub）。
    千问（百炼）不用 reasoning_effort（它用 thinking_budget 限思考长度），不在这里。"""
    u = str(spec.get("base_url", "")).lower()
    return any(k in u for k in ("deepseek", "moonshot", "volces", "bigmodel", "tencentmaas"))


def _think_fixed(spec):
    """思考始终开启、关不掉、只能调强度：Kimi K3；2.6 加智谱 GLM-5.3（官方文档）。"""
    m = str(spec.get("model", "")).lower()
    return "kimi-k3" in m or m.startswith("glm-5.3")


def _qwen_style(spec):
    """2.6：千问（阿里百炼）用 enable_thinking: true/false 开关思考，不认 thinking.type。"""
    u = str(spec.get("base_url", "")).lower()
    return "dashscope" in u or "maas.aliyuncs" in u


def _thinking_on(spec):
    ex = spec.get("extra") or {}
    if _qwen_style(spec):
        return ex.get("enable_thinking") is not False
    return (ex.get("thinking") or {}).get("type") != "disabled"


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
                running = bool(self.running_pid(d))
                out.append({"name": d.name, "running": running, "done": done, **self.talk_state(d, running)})
        return out

    # ---------- 2.9：主窗口输入框（建题、插话、暂停） ----------
    def running_cmd(self, proj: Path) -> str:
        try:
            parts = (proj / LOCK).read_text(encoding="utf-8").split()
            return parts[1] if len(parts) > 1 else "run"
        except OSError:
            return ""

    def talk_state(self, proj: Path, running: bool) -> dict:
        """暂停状态：pausing＝运行中、已请求暂停、等这一步跑完；paused＝已暂停、等你补充；finished＝流程已结束。"""
        import sqlite3
        st = {"paused": False, "pausing": False, "finished": False, "started": False, "pending": len(I.pending(proj))}
        try:
            c = sqlite3.connect(f"file:{(proj / 'project.db').as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                st["finished"] = bool(c.execute("SELECT 1 FROM events WHERE type='迭代结束' LIMIT 1").fetchone())
                st["started"] = bool(c.execute("SELECT 1 FROM calls LIMIT 1").fetchone())
                row = c.execute("SELECT type FROM events WHERE type NOT IN ('用户指令', '运行开始') ORDER BY seq DESC LIMIT 1").fetchone()
            finally:
                c.close()
        except sqlite3.Error:
            return st
        if running:
            st["pausing"] = I.pause_requested(proj)
            st["cmd"] = self.running_cmd(proj)
        else:
            st["paused"] = bool(row and row[0] == "已暂停")
        return st

    def say(self, name: str, text: str):
        """输入框发话。没有选项目＝用题面新建项目；运行中＝插话并请求暂停；暂停中／还没开始＝直接记入。"""
        from .store import Project
        proj = self.base / name
        if not (proj / "project.db").exists():
            return {"ok": False, "msg": "找不到这个项目"}
        text = (text or "").strip()
        if not text:
            return {"ok": False, "msg": "内容是空的"}
        running = bool(self.running_pid(proj))
        ts = self.talk_state(proj, running)
        if ts["finished"]:
            return {"ok": False, "msg": "这个项目的流程已经结束，补充的话不会再送达任何角色。要换个问法，请在输入框里新建一道题。"}
        if running:
            if self.running_cmd(proj) != "run":
                return {"ok": False, "msg": "现在在做裸题对照，不能插话；等它做完再说。"}
            try:
                I.post(proj, text)
            except ValueError as e:
                return {"ok": False, "msg": str(e)}
            return {"ok": True, "msg": "已收到。等正在跑的这一步全部返回后暂停，暂停后你可以接着补充。"}
        pj = Project(proj).open()
        try:
            if ts["paused"]:
                I.say_while_paused(pj, text)
                return {"ok": True, "msg": "已记下。补充完点「继续运行」，所有角色都会收到。"}
            if not ts["started"]:  # 还没开始：从第 1 轮第一步起生效
                I.log_instruction(pj, text, time.strftime("%Y-%m-%dT%H:%M:%S"), (1, 1), "开始运行前补充")
                return {"ok": True, "msg": "已记下。开始运行后所有角色都会收到。"}
            return {"ok": False, "msg": "这个项目是被停止的，不在暂停中。先点「继续运行」，运行中再发话：会等这一步跑完后暂停，再送达所有角色。"}
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        finally:
            pj.close()

    def interrupt(self, name: str):
        """2.9.1 打断（一级）：请求暂停，等正在跑的调用全部返回，一个都不浪费。"""
        proj = self.base / name
        if not self.running_pid(proj):
            return {"ok": False, "msg": "这个项目现在没有在运行"}
        if self.running_cmd(proj) != "run":
            return {"ok": False, "msg": "现在在做裸题对照，不能打断；要停就用「停止运行」。"}
        (proj / I.FLAG).write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        return {"ok": True}

    def inflight(self, name: str):
        """正在等的调用：给「直接打断」的确认弹窗用（谁、等了多久）。"""
        import sqlite3
        proj = self.base / name
        try:
            c = sqlite3.connect(f"file:{(proj / 'project.db').as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                rows = c.execute("SELECT role, call_key, started_ts FROM live_calls").fetchall()
            finally:
                c.close()
        except sqlite3.Error:
            rows = []
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        out = []
        for role, key, ts in rows:
            try:  # started_ts 是 UTC 时间（store.now_iso）
                secs = int((now - _dt.datetime.fromisoformat(ts)).total_seconds())
            except (TypeError, ValueError):
                secs = None
            out.append({"role": role, "call_key": key, "label": feed.call_label(key), "seconds": secs})
        return out

    def interrupt_now(self, name: str):
        """2.9.1 直接打断（二级）：掐断进程，正在跑的调用作废、不补；进入暂停，可以补充后继续。「停止运行」与此无关。"""
        from .store import Project
        proj = self.base / name
        pid = self.running_pid(proj)
        if not pid:
            return {"ok": False, "msg": "这个项目现在没有在运行"}
        if self.running_cmd(proj) != "run":
            return {"ok": False, "msg": "现在在做裸题对照，不能打断；要停就用「停止运行」。"}
        fl = self.inflight(name)
        keys = [x["call_key"] for x in fl]
        if any(k.startswith(I.FORM_PREFIX) for k in keys):
            return {"ok": False, "msg": "表单阶段不支持直接打断，请用「停止运行」。"}
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NOWIN)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except OSError as e:
            return {"ok": False, "msg": f"没能打断：{e}"}
        for _ in range(50):
            if not _alive(pid):
                break
            time.sleep(0.1)
        (proj / LOCK).unlink(missing_ok=True)
        pj = Project(proj).open()
        try:
            pj.save_unfinished("用户直接打断")  # 2.11：作废的调用已收到的内容先存进账本，只供查看
            try:
                r = I.direct_interrupt(pj, proj, keys)
            except ValueError as e:
                pj.log("用户", "用户停止运行", content={"说明": f"用户点了直接打断，但{e}；按停止处理"})
                return {"ok": False, "msg": f"已停下，但{e}。"}
        finally:
            pj.close()
        try:
            with open(proj / LOG, "ab") as f:
                f.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 用户直接打断（已暂停）====\n".encode("utf-8"))
        except OSError:
            pass
        return {"ok": True, "msg": f"已直接打断，{I.pos_text(r['位置'])}暂停。"}

    # ---------- 2.10 解说者 ----------
    def explain_view(self, name):
        from . import explainer
        try:
            st = load_settings(self.base / SETTINGS)
        except (OSError, ValueError):
            st = {"roles": {}}
        role, spec = explainer.find_role(st)
        return {"configured": bool(role), "role": role, "model": (spec or {}).get("model", ""),
                "name": (spec or {}).get("series", role or ""),
                "history": [{"who": h["who"], "text": h["text"], "ts": h.get("ts", "")}
                            for h in explainer.history(self.base, name or None)]}

    def explain_ask(self, name, text):
        from . import explainer
        if name and not (self.base / name / "project.db").exists():
            return {"ok": False, "msg": "找不到这个项目"}
        try:
            st = load_settings(self.base / SETTINGS)
            sec = load_secrets(self.base / SECRETS)
        except (OSError, ValueError) as e:
            return {"ok": False, "msg": f"设置文件读不出来：{e}"}
        running = bool(name) and bool(self.running_pid(self.base / name))
        return explainer.ask(self.base, name or None, text, st, sec, running)

    def quick_new(self, text: str):
        """输入框建题：一个框，只放题面（口径写在题面里），不填标准答案。"""
        from .tasks.final_answer import SCOPE_IN_PROBLEM
        text = (text or "").strip()
        if not text:
            return {"ok": False, "msg": "题面是空的"}
        if len(text) > I.MAX_LEN:
            return {"ok": False, "msg": f"题面最多 {I.MAX_LEN} 字"}
        stamp = time.strftime("%m%d_%H%M%S")
        name = f"题目_{stamp}"
        k = 1
        while (self.base / name).exists():
            k += 1
            name = f"题目_{stamp}_{k}"
        first = " ".join(text.split())[:40]
        cfg = {"project_id": f"Q{time.strftime('%Y%m%d%H%M%S')}", "question": first, "scope": SCOPE_IN_PROBLEM,
               "task": "final_answer", "source": "主窗口输入框",
               "items": [{"id": "Q1", "name": first, "problem": text}]}
        tmpq = self.base / f".tmp_say_{os.getpid()}.json"
        tmpq.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        try:
            return self._create(name, ["new", name, "--project", str(tmpq)])
        finally:
            tmpq.unlink(missing_ok=True)

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
                            "trusted": bool(obj.get("trusted_sites")), "task": obj.get("task"),
                            "items": self._item_labels(obj)})
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
                            "count": len(obj["items"]), "trusted": bool(obj.get("trusted_sites")), "task": obj.get("task"),
                            "items": self._item_labels(obj)})
        return out

    @staticmethod
    def _item_labels(obj):
        """2.2：列出全部题号，供新建项目时勾选（只对最终答案题给；其他题目包不用）。"""
        if obj.get("task") != "final_answer":
            return None
        out = []
        for it in obj["items"]:
            if isinstance(it, dict) and "id" in it:
                lab = str(it.get("name") or it.get("problem") or "")[:40].replace("\n", " ")
                out.append({"id": str(it["id"]), "label": lab})
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
        if not hasattr(self, "_ch"):
            from . import __version__
            from .store import code_hashes
            ch = code_hashes()
            self._ch = {"version": __version__, "code_hash": ch["__total__"], "files": len(ch) - 1}
        return {**self._ch, "data_dir": str(self.base), "moved": self.moved, "roles": roles, "max_rounds": max_rounds, "search_key": search_key, "secrets_file": (self.base / SECRETS).exists(),
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

    def new_project(self, name: str, qfile: str, picked=None):
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
            if picked is not None:  # 2.2：只做勾选的题；写一个临时的子集题目文件再创建
                try:
                    obj = json.loads(self.question_path(qfile).read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    return {"ok": False, "msg": "读不出这个题目文件"}
                if not isinstance(picked, list) or not all(isinstance(x, str) for x in picked):
                    return {"ok": False, "msg": "收到的题号格式不对"}
                have = {str(it.get("id")): it for it in obj["items"] if isinstance(it, dict)}
                unknown = [x for x in picked if x not in have]
                if unknown:
                    return {"ok": False, "msg": "题目文件里没有这些题号：" + "、".join(unknown[:5])}
                if not picked:
                    return {"ok": False, "msg": "至少要勾选一道题"}
                sub = dict(obj, items=[have[x] for x in dict.fromkeys(picked)])
                sub["scope"] = f"{obj.get('scope', '')}（本项目只做题库 {len(have)} 题中勾选的 {len(sub['items'])} 题）"
                tmpq = self.base / f".tmp_q_{os.getpid()}.json"
                tmpq.write_text(json.dumps(sub, ensure_ascii=False), encoding="utf-8")
                args = ["new", name, "--project", str(tmpq)]
        try:
            return self._create(name, args)
        finally:
            if picked is not None and qfile != "__demo__":
                (self.base / f".tmp_q_{os.getpid()}.json").unlink(missing_ok=True)

    def _create(self, name, args):
        r = subprocess.run([*_cmd(), *args], cwd=self.base, capture_output=True,
                           env=self.env(), creationflags=NOWIN)
        out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        if r.returncode != 0:
            return {"ok": False, "msg": out[-400:] or "创建失败"}
        return {"ok": True, "name": name}

    def run(self, name: str, cmd: str = "run"):
        proj = self.base / name
        if not (proj / "project.db").exists():
            return {"ok": False, "msg": "找不到这个项目"}
        if self.running_pid(proj):
            return {"ok": False, "msg": "这个项目已经在运行了"}
        log = open(proj / LOG, "ab")
        log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {'开始运行' if cmd == 'run' else '开始裸题对照'} ====\n".encode("utf-8"))
        log.flush()
        p = subprocess.Popen([*_cmd(), cmd, name, "--settings", SETTINGS, "--secrets", SECRETS],
                             cwd=self.base, stdout=log, stderr=subprocess.STDOUT,
                             env=self.env(), creationflags=NOWIN)
        (proj / LOCK).write_text(f"{p.pid} {cmd}\n", encoding="utf-8")  # 2.9：记下在跑什么（裸题对照时不能插话）

        def reap():
            p.wait()
            log.close()
            (proj / LOCK).unlink(missing_ok=True)
        threading.Thread(target=reap, daemon=True).start()
        return {"ok": True}

    def stop(self, name: str):
        """2.1.2：停止这个项目的运行（结束运行进程）。已完成的调用都在账本里，之后点"继续运行"即可接着跑；
        只有停下那一刻正在等待回复的那次调用会作废。"""
        from .store import Project
        proj = self.base / name
        if not (proj / "project.db").exists():
            return {"ok": False, "msg": "找不到这个项目"}
        pid = self.running_pid(proj)
        if not pid:
            return {"ok": False, "msg": "这个项目现在没有在运行"}
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NOWIN)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except OSError as e:
            return {"ok": False, "msg": f"没能停下：{e}"}
        for _ in range(50):  # 最多等 5 秒，让进程真正退出
            if not _alive(pid):
                break
            time.sleep(0.1)
        (proj / LOCK).unlink(missing_ok=True)
        (proj / I.FLAG).unlink(missing_ok=True)  # 2.9：停止就不再等暂停；待送达的话留着，下次运行时送达
        try:
            pj = Project(proj).open()
            pj.save_unfinished("用户停止运行")  # 2.11：还在进行的调用已收到的内容先存进账本，只供查看
            pj.log("用户", "用户停止运行", content={"说明": "用户点了停止；已完成的调用保留，继续运行时从停下处接着做"})
            try:  # 2.4（规格 10.3）：项目被终止时同样出结论：结论＝未得出，另写终止原因；已完成的部分照常列出
                from . import report as _report
                _report.build(pj)
            except Exception:  # noqa: BLE001  出结论失败不能妨碍停止本身
                pass
            pj.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(proj / LOG, "ab") as f:
                f.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 用户停止运行 ====\n".encode("utf-8"))
        except OSError:
            pass
        return {"ok": True}

    def trusted_ok(self, name: str) -> bool:
        """1.5：项目的问题里（或设置里）有没有可信网站名单；没有的话独立核验不会启动。"""
        try:
            cfg = json.loads((self.base / name / "config.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            cfg = {}
        if cfg.get("trusted_sites") or cfg.get("task") == "final_answer":  # 2.1：最终答案题不检索，不需要可信网站
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

    # ---------- 2.1：建题窗口 ----------
    def adm_check(self, filename, text, mapping, items):
        """读入文件（或用已有条目）→ 字段对应 → 自检。"""
        from . import admission as A
        try:
            if items is None:
                rows = A.read_table(text, filename)
                cols = A.columns_of(rows)
                mp = mapping or A.guess_mapping(cols)
                if not mp.get("problem") or not mp.get("gold"):
                    return {"ok": True, "stage": "map", "columns": cols, "mapping": mp, "count": len(rows),
                            "preview": rows[:3]}
                items = A.to_items(rows, mp)
            else:
                if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                    return {"ok": False, "msg": "题目列表格式不对"}
                items = [{"id": str(x.get("id", "")), "name": str(x.get("name", "")), "problem": str(x.get("problem", "")),
                          "gold": str(x.get("gold", ""))} for x in items]
        except (ValueError, KeyError, TypeError) as e:
            return {"ok": False, "msg": f"读取失败：{e}"}
        checks = A.self_check(items)
        return {"ok": True, "stage": "check", "items": items, "checks": checks, "summary": A.summary(checks, len(items)),
                "mapping": mapping or {}}

    def adm_make(self, body):
        from . import admission as A
        from .store import Project
        try:
            items = body.get("items") or []
            cfg = A.build_config(str(body.get("project_id", "")).strip(), str(body.get("question", "")).strip() or "数学题",
                                 str(body.get("scope", "")).strip() or "只给最终答案；不联网；是否运行验证代码见参数面板",
                                 items, source=str(body.get("source", "")), license_=str(body.get("license", "")),
                                 acknowledged=body.get("acknowledged") or [])
            path = A.write_question_file(self.base, cfg)
        except (ValueError, FileExistsError, TypeError, AttributeError) as e:
            return {"ok": False, "msg": str(e)}
        r = self.new_project(cfg["project_id"], path.name)
        if not r.get("ok"):
            return r
        try:  # 准入记录写进账本：用户自己提交、自检结果、已知晓的红色问题
            pj = Project(self.base / cfg["project_id"]).open()
            red = [c for c in cfg["admission"]["自检"] if c["level"] == "红"]
            pj.log("用户", "准入判断", content={"结果": "用户提交", "题数": len(items), "红": len(red),
                                          "黄": sum(c["level"] == "黄" for c in cfg["admission"]["自检"]),
                                          "已知晓的红色问题": cfg["admission"]["已知晓的红色问题"],
                                          "来源": cfg["admission"]["来源"], "许可": cfg["admission"]["许可"]})
            pj.close()
        except Exception as e:  # noqa: BLE001
            return {"ok": True, "name": cfg["project_id"], "msg": f"项目已创建，但准入记录没写入：{e}"}
        return {"ok": True, "name": cfg["project_id"], "file": path.name}

    # ---------- 2.1：待人工判断 ----------
    def pending(self, name):
        from .store import Project
        from .tasks import final_answer as T
        pj = Project(self.base / name).open()
        try:
            return T.pending(pj)
        finally:
            pj.close()

    def judge(self, name, key, verdict, note):
        from .store import Project
        from .tasks import final_answer as T
        if verdict not in ("对", "错"):
            return {"ok": False, "msg": "判断只能是 对 或 错"}
        pj = Project(self.base / name).open()
        try:
            if key not in {x["key"] for x in T.pending(pj)}:
                return {"ok": False, "msg": "这一项已经判过，或不在待判清单里"}
            pj.log("用户", "人工判断", ref_type="key", ref_id=key, content={"verdict": verdict, "note": str(note or "")[:500]})
            from . import report
            report.build(pj)
        finally:
            pj.close()
        return {"ok": True}


    # ---------- 2.7：表单确认、裸题对照、错误归因、配置面板 ----------
    def forms(self, name):
        from .flow_form import form_candidates, frozen_forms
        from .store import Project
        pj = Project(self.base / name).open()
        try:
            cands, fro = form_candidates(pj), frozen_forms(pj)
            out = []
            for it in pj.config.get("items", []):
                iid = it["id"]
                c, f = cands.get(iid), fro.get(iid)
                started = bool(pj.q("SELECT 1 FROM events WHERE type='提交' AND ref_id LIKE ?", (f"{iid}|%",)))
                out.append({"id": iid, "name": it.get("name") or iid, "题面": it.get("problem", ""),
                            "状态": "已冻结" if f else ("待确认" if c else "表单阶段未结束"),
                            "表单阶段": (c or {}).get("状态"), "原因": (c or {}).get("原因"), "轮数": (c or {}).get("轮数"),
                            "候选": (c or {}).get("候选"), "各方表单": (c or {}).get("各方表单"),
                            "补词": (c or {}).get("补词（含模型判断）"), "提示": (c or {}).get("程序提示"),
                            "冻结版": (f or {}).get("冻结版"), "可改": bool(c) and not started})
            fs = pj.q("SELECT content FROM events WHERE type='表单阶段设置' ORDER BY seq LIMIT 1")
            use_form = pj.config.get("flow_version") == "2.7" or bool(fs and json.loads(fs[0]["content"]).get("开启"))
            return {"ok": True, "items": out, "math": pj.config.get("flow_version") in ("2.7", "2.8", "2.9"), "form": use_form,
                    "all_frozen": bool(out) and all(x["状态"] == "已冻结" for x in out),
                    "waiting": any(x["状态"] == "待确认" for x in out)}
        finally:
            pj.close()

    def form_confirm(self, name, item, form, note=""):
        from .flow_form import confirm_form
        from .store import Project
        if self.running_pid(self.base / name):
            return {"ok": False, "msg": "项目正在运行，等它停下再确认"}
        pj = Project(self.base / name).open()
        try:
            r = confirm_form(pj, str(item), form, note)
        except (ValueError, TypeError, AttributeError) as e:
            return {"ok": False, "msg": str(e)}
        finally:
            pj.close()
        return {"ok": True, **r}

    def form_confirm_all(self, name):
        """未修改的一次全部确认（批量题：表单全部做完后一次确认）。已冻结的不动。"""
        v = self.forms(name)
        done, msgs = 0, []
        for x in v["items"]:
            if x["状态"] == "待确认" and x["可改"]:
                r = self.form_confirm(name, x["id"], x["候选"], "一次全部确认")
                if r.get("ok"):
                    done += 1
                else:
                    msgs.append(f"{x['id']}：{r.get('msg')}")
        return {"ok": not msgs, "done": done, "msg": "；".join(msgs)}

    def bare(self, name):
        return self.run(name, "bare")

    def attr_list(self, name):
        from .store import Project
        pj = Project(self.base / name).open()
        try:
            fin = {r["ref_id"]: json.loads(r["content"]) for r in pj.q("SELECT ref_id, content FROM events WHERE type='最终状态'")}
            at = {r["ref_id"]: json.loads(r["content"]) for r in pj.q("SELECT ref_id, content FROM events WHERE type='错误归因' ORDER BY seq")}
            return {"items": [{"id": it["id"], "状态": (fin.get(it["id"]) or {}).get("状态"), "归因": (at.get(it["id"]) or {}).get("归因"),
                               "note": (at.get(it["id"]) or {}).get("note", "")} for it in pj.config.get("items", []) if it["id"] in fin]}
        finally:
            pj.close()

    def attr_set(self, name, item, value, note):
        from .store import Project
        if value not in ("表单正确、错在做题", "表单有误", "判断不了"):
            return {"ok": False, "msg": "只能选：表单正确、错在做题／表单有误／判断不了"}
        pj = Project(self.base / name).open()
        try:
            if not pj.q("SELECT 1 FROM events WHERE type='最终状态' AND ref_id=?", (item,)):
                return {"ok": False, "msg": "这道题还没有结果"}
            pj.log("用户", "错误归因", ref_type="key", ref_id=item, content={"归因": value, "note": str(note or "")[:500]})
            from . import report
            report.build(pj)
        finally:
            pj.close()
        return {"ok": True}

    def configs(self):
        """配置面板：数据文件夹里的设置文件（含备份）和 examples 里的示例配置，都可以直接换上。"""
        out = []
        for where, d in (("data", self.base), ("examples", self.prog / "examples")):
            for f in sorted(Path(d).glob("*.json")) if Path(d).exists() else []:
                try:
                    obj = json.loads(f.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    continue
                if not isinstance(obj, dict) or not isinstance(obj.get("roles"), dict):
                    continue
                roles = "；".join(f"{r}={v.get('series', '')}（{v.get('kind', '')}）" for r, v in obj["roles"].items())
                out.append({"key": f"{where}:{f.name}", "where": "数据文件夹" if where == "data" else "示例",
                            "name": f.name, "current": where == "data" and f.name == SETTINGS, "roles": roles,
                            "note": str(obj.get("_说明", ""))[:200]})
        return {"ok": True, "items": out}

    def config_apply(self, key):
        where, _, fname = str(key or "").partition(":")
        d = {"data": self.base, "examples": self.prog / "examples"}.get(where)
        if not d or not SAFE.match(fname) or "/" in fname or "\\" in fname or not fname.endswith(".json"):
            return {"ok": False, "msg": "选的配置不对"}
        src = Path(d) / fname
        if not src.exists():
            return {"ok": False, "msg": "找不到这个配置文件"}
        if where == "data" and fname == SETTINGS:
            return {"ok": False, "msg": "这就是现在用的配置"}
        try:
            obj = json.loads(src.read_text(encoding="utf-8-sig"))
            if not isinstance(obj.get("roles"), dict):
                raise ValueError("没有 roles")
        except (OSError, ValueError) as e:
            return {"ok": False, "msg": f"这个配置文件读不出来：{e}"}
        cur = self.base / SETTINGS
        bak = None
        if cur.exists():  # 换上前自动备份
            bak = self.base / f"设置_备份_{time.strftime('%Y%m%d-%H%M%S')}.json"
            shutil.copy2(cur, bak)
        shutil.copy2(src, cur)
        return {"ok": True, "backup": bak.name if bak else None}

    # ---------- 2.1：参数面板 ----------
    def params_view(self):
        from .config import PARAM_SCHEMA, SEARCH_ENGINES
        try:
            st = load_settings(self.base / SETTINGS)
        except (FileNotFoundError, ValueError) as e:
            return {"ok": False, "msg": f"设置文件读不出来：{e}"}
        return {"ok": True, "schema": [dict(zip(("key", "label", "type", "min", "max", "desc", "live"), x)) for x in PARAM_SCHEMA],
                "values": st["params"], "engines": [dict(zip(("key", "label", "live"), x)) for x in SEARCH_ENGINES],
                "engine": st.get("search", {}).get("engine", "tavily"),
                "roles": [{"role": r, "name": v.get("series", r), "cutoff": v.get("knowledge_cutoff", ""),
                           "thinking": _thinking_on(v),
                           "effort": (v.get("extra") or {}).get("reasoning_effort", ""),
                           "effort_ok": _effort_ok(v), "thinking_fixed": _think_fixed(v)}
                          for r, v in st["roles"].items()]}

    def set_params(self, params, engine, cutoffs, thinking=None):
        import re as _re
        from .config import PARAM_SCHEMA, SEARCH_ENGINES
        path = self.base / SETTINGS
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"ok": False, "msg": "找不到 设置.json"}
        except ValueError as e:
            return {"ok": False, "msg": f"设置.json 格式有误，没有改动：{e}"}
        if not isinstance(obj, dict) or not isinstance(obj.get("roles"), dict):
            return {"ok": False, "msg": "设置.json 结构不对，没有改动"}
        if not isinstance(params, dict) or not isinstance(cutoffs or {}, dict) or not isinstance(engine, (str, type(None))):
            return {"ok": False, "msg": "收到的内容格式不对，没有改动"}
        newp = {}
        for key, label, typ, lo, hi, _d, _live in PARAM_SCHEMA:
            if key not in params:
                continue
            v = params[key]
            if typ == "choice":
                if v not in lo:
                    return {"ok": False, "msg": f"「{label}」只能选：{'、'.join(lo)}"}
                newp[key] = v
                continue
            if typ == "bool":
                if isinstance(v, str):
                    v = v.strip().lower() in ("true", "1", "是", "on")
                newp[key] = bool(v)
                continue
            if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, str) and re.fullmatch(r"\s*\d+\s*", v))):
                return {"ok": False, "msg": f"「{label}」要填整数"}
            n = int(v)
            if not lo <= n <= hi:
                return {"ok": False, "msg": f"「{label}」只能填 {lo} 到 {hi}"}
            newp[key] = n
        if engine is not None:
            if engine not in {e[0] for e in SEARCH_ENGINES if e[2]}:
                return {"ok": False, "msg": "这个检索服务还没有接入，不能选"}
        for r, c in (cutoffs or {}).items():
            if r not in (obj.get("roles") or {}):
                return {"ok": False, "msg": f"设置里没有角色 {r}"}
            if c and not _re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(c)):
                return {"ok": False, "msg": f"{r} 的模型截止时间要写成 2025-06 这样的年月（不确定就留空）"}
        if thinking is not None and not isinstance(thinking, dict):
            return {"ok": False, "msg": "收到的内容格式不对，没有改动"}
        for r, t in (thinking or {}).items():
            if r not in obj["roles"] or not isinstance(t, dict):
                return {"ok": False, "msg": f"设置里没有角色 {r}"}
            if t.get("effort") not in (None, "", "low", "medium", "high", "max"):
                return {"ok": False, "msg": "思考强度只能是 low、medium、high 或 max"}
        if path.exists():
            shutil.copy2(path, path.with_name(SETTINGS + ".bak"))
        for r, t in (thinking or {}).items():  # 2.1.2：深度思考开关（三家接口都用 thinking.type），DeepSeek 另有强度
            spec = obj["roles"][r]
            ex = spec.get("extra") if isinstance(spec.get("extra"), dict) else {}
            if _qwen_style(spec):  # 2.6 千问：用 enable_thinking，不传 thinking.type
                ex.pop("thinking", None)
                ex["enable_thinking"] = bool(t.get("on"))
            elif _think_fixed(spec):  # Kimi K3：思考关不掉，不传 thinking.type，只调强度
                if "kimi" in str(spec.get("model", "")).lower():
                    ex.pop("thinking", None)
                else:  # GLM-5.3：官方示例写 thinking.type=enabled，关不掉
                    ex["thinking"] = {"type": "enabled"}
            else:
                ex["thinking"] = {"type": "enabled" if t.get("on") else "disabled"}
            if (t.get("on") or _think_fixed(spec)) and t.get("effort") and _effort_ok(spec):
                ex["reasoning_effort"] = t["effort"]
            else:
                ex.pop("reasoning_effort", None)
            spec["extra"] = ex
        if not isinstance(obj.get("params"), dict):
            obj["params"] = {}
        obj["params"].update(newp)
        if engine is not None:
            obj.setdefault("search", {})["engine"] = engine
        for r, c in (cutoffs or {}).items():
            if c:
                obj["roles"][r]["knowledge_cutoff"] = str(c)
            else:
                obj["roles"][r].pop("knowledge_cutoff", None)
        tmp = path.with_name(SETTINGS + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return {"ok": True, "msg": "已保存到 设置.json（改之前的版本备份为 设置.json.bak）。下次运行时生效，正在运行的项目不受影响。"}

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
            if u.path in ("/favicon.ico", "/icon.png"):  # 2.11：程序图标（随程序附带，不联网）
                f = Path(__file__).parent / "assets" / ("icon.ico" if u.path.endswith(".ico") else "icon_128.png")
                try:
                    return self._send(f.read_bytes(), "image/x-icon" if u.path.endswith(".ico") else "image/png")
                except OSError:
                    return self._send(b"", "text/plain", 404)
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
                r["role_states"] = feed.role_states(app.base / name, r["running"])  # 2.0 界面待办：左栏头像状态
                r["role_usage"] = feed.role_usage(app.base / name)  # 2.0 界面待办：左栏头像下方用量
                r["timing"] = feed.timing(app.base / name, r["running"])  # 2.10：开始/结束时间、总用时
                r["talk"] = dict(app.talk_state(app.base / name, r["running"]), waiting=[  # 2.9：暂停提示与待送达的话
                    k for k, v in (r["role_states"] or {}).items() if v.get("state") in ("工作中", "响应中")],
                    pending_msgs=[{"text": m["text"], "ts": m.get("ts", "")} for m in I.pending(app.base / name)],
                    inflight=app.inflight(name) if r["running"] else [])
                if (q.get("think") or ["0"])[0] == "1" and r["running"]:
                    r["live_thinking"] = feed.live_thinking(app.base / name)  # 2.5：实时思考（界面开了"显示思考过程"才取）
                try:
                    r["question"] = json.loads((app.base / name / "config.json").read_text(encoding="utf-8-sig"))["question"]
                except (OSError, ValueError, KeyError):
                    r["question"] = ""
                return self._send(r)
            if u.path == "/api/thinking":
                name = self._proj(q)
                txt = feed.call_thinking(app.base / name, (q.get("call") or [""])[0]) if name else None
                return self._send({"text": txt})
            if u.path == "/api/pending":
                name = self._proj(q)
                return self._send({"items": app.pending(name) if name else []})
            if u.path == "/api/params":
                return self._send(app.params_view())
            if u.path == "/api/forms":
                name = self._proj(q)
                return self._send(app.forms(name) if name else {"ok": False, "items": []})
            if u.path == "/api/attr":
                name = self._proj(q)
                return self._send(app.attr_list(name) if name else {"items": []})
            if u.path == "/api/configs":
                return self._send(app.configs())
            if u.path == "/api/explain":  # 2.10
                name = (q.get("p") or [""])[0]
                if name and not self._proj(q):
                    return self._send({"configured": False, "history": []})
                return self._send(app.explain_view(name))
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
            if self.headers.get("X-AlreadyThere") != "1":  # 只接受本窗口发来的请求
                return self._send({"ok": False, "msg": "拒绝"}, code=403)
            if u.path == "/api/ping":
                return self._send(app.ping())
            if u.path == "/api/explain":  # 2.10
                name = str(body.get("name") or "")
                if name and not SAFE.match(name):
                    return self._send({"ok": False, "msg": "项目名不对"})
                return self._send(app.explain_ask(name, str(body.get("text") or "")))
            if u.path in ("/api/interrupt", "/api/interrupt_now"):  # 2.9.1
                name = str(body.get("name") or "")
                if not SAFE.match(name) or not (app.base / name / "project.db").exists():
                    return self._send({"ok": False, "msg": "找不到这个项目"})
                return self._send(app.interrupt(name) if u.path == "/api/interrupt" else app.interrupt_now(name))
            if u.path == "/api/say":  # 2.9
                name = str(body.get("name") or "")
                if not name:
                    return self._send(app.quick_new(str(body.get("text") or "")))
                if not SAFE.match(name):
                    return self._send({"ok": False, "msg": "项目名不对"})
                return self._send(app.say(name, str(body.get("text") or "")))
            if u.path == "/api/new":
                return self._send(app.new_project(str(body.get("name", "")).strip(), str(body.get("question", "")),
                                                  body.get("picked")))
            if u.path == "/api/stop":
                return self._send(app.stop(str(body.get("name", ""))))
            if u.path == "/api/run":
                return self._send(app.run(str(body.get("name", ""))))
            if u.path == "/api/memory":
                return self._send(app.set_memory(body.get("roles")))
            if u.path == "/api/adm/check":
                return self._send(app.adm_check(str(body.get("filename", "")), str(body.get("text", "")),
                                                body.get("mapping"), body.get("items")))
            if u.path == "/api/adm/make":
                return self._send(app.adm_make(body))
            if u.path == "/api/judge":
                nm = str(body.get("name", ""))
                if not SAFE.match(nm) or not (app.base / nm / "project.db").exists():
                    return self._send({"ok": False, "msg": "找不到项目"})
                return self._send(app.judge(nm, str(body.get("key", "")), str(body.get("verdict", "")), body.get("note")))
            if u.path == "/api/params":
                return self._send(app.set_params(body.get("params"), body.get("engine"), body.get("cutoffs"), body.get("thinking")))
            if u.path == "/api/detail":
                return self._send(app.detail(str(body.get("name", ""))))
            nm = str(body.get("name", ""))
            okp = SAFE.match(nm) and (app.base / nm / "project.db").exists()
            if u.path == "/api/form/confirm":
                return self._send(app.form_confirm(nm, body.get("item"), body.get("form"), body.get("note")) if okp
                                  else {"ok": False, "msg": "找不到项目"})
            if u.path == "/api/form/confirm_all":
                return self._send(app.form_confirm_all(nm) if okp else {"ok": False, "msg": "找不到项目"})
            if u.path == "/api/bare":
                return self._send(app.bare(nm) if okp else {"ok": False, "msg": "找不到项目"})
            if u.path == "/api/attr":
                return self._send(app.attr_set(nm, str(body.get("item", "")), str(body.get("value", "")), body.get("note"))
                                  if okp else {"ok": False, "msg": "找不到项目"})
            if u.path == "/api/configs/apply":
                return self._send(app.config_apply(body.get("key")))
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
    # 2.11 起包名是 alreadythere；2.10 及以前的程序文件夹里是 guanzhe（换代时要能认出旧版，才能把设置、密钥、题目、项目搬过来）
    return any((d / n / "__init__.py").exists() for n in ("alreadythere", "guanzhe")) or (d / "AlreadyThere.exe").exists()


def _version_key(d: Path):
    """程序文件夹名里的版本号 -> 可比较的三元组。2.1 起认三段式（X.Y 或 X.Y.Z）：
    "_v2.0" 读成 (2,0,0)，"_v2.0.1" 读成 (2,0,1)，所以 2.0.1 比 2.0 新；认不出来的排最后 (0,0,0)。
    注意：版本号后面必须是文件夹名的结尾或非数字，避免把 "_v2.0.10" 误读成 2.0.1。"""
    import re as _re
    m = _re.search(r"_v(\d+)\.(\d+)(?:\.(\d+))?(?!\d)", d.name)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else (0, 0, 0)


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
