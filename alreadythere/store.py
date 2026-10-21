"""项目存储：一个项目 = 一个文件夹（9.9）。

文件夹结构：
  <项目>/project.db        SQLite：账本及各记录表
  <项目>/config.json       项目配置（问题、口径、角色、参数）
  <项目>/archive/          检索原文存档（按哈希命名）
  <项目>/roles/<角色编号>/  各角色产物（程序命名，9.6）

账本（事件表）只追加，每条事件包含上一条的哈希（9.1）。所有写入由程序完成，模型不接触存档（9.7）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .util import canonical_json, now_iso, sha256_text

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY,
  event_id TEXT UNIQUE NOT NULL,
  ts TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL,
  round INTEGER,
  phase TEXT,
  actor TEXT NOT NULL,
  type TEXT NOT NULL,
  ref_type TEXT,
  ref_id TEXT,
  content TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  call_id TEXT PRIMARY KEY,
  event_id TEXT,
  role TEXT, session_id TEXT, provider TEXT, model TEXT, series TEXT,
  params TEXT, request TEXT, request_hash TEXT, response TEXT,
  input_tokens INTEGER, output_tokens INTEGER, context_ratio REAL,
  status TEXT, retry_batch INTEGER, retry_seq INTEGER, failure TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS retrievals (
  retrieval_id TEXT PRIMARY KEY,
  requester TEXT, item_id TEXT, round INTEGER, ts TEXT,
  query TEXT, url TEXT, title TEXT,
  archive_path TEXT, content_hash TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  author TEXT, round INTEGER, type TEXT, filename TEXT,
  output_hash TEXT, input_hashes TEXT, base_hash TEXT,
  event_id TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS blocks (
  block_id TEXT PRIMARY KEY,
  artifact_id TEXT, author TEXT, round INTEGER, block_type TEXT, item_id TEXT,
  fields TEXT,
  st_check TEXT, st_verify TEXT, st_indep TEXT, st_valid TEXT,
  depends_on TEXT, flags TEXT
);
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  name TEXT, type TEXT,
  st_check TEXT, st_verify TEXT, st_indep TEXT, st_valid TEXT,
  indep_count INTEGER DEFAULT 0,
  value TEXT, note TEXT, history TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  reviewer TEXT, artifact_id TEXT, author TEXT, round INTEGER,
  same_model INTEGER, item_id TEXT, block_id TEXT,
  verdict TEXT, severity TEXT, method TEXT, evidence TEXT,
  reasoning TEXT, changes_state INTEGER, opinion_id TEXT
);
CREATE TABLE IF NOT EXISTS opinions (
  opinion_id TEXT PRIMARY KEY,
  source_role TEXT, review_id TEXT, target_author TEXT, item_id TEXT,
  content TEXT, status TEXT, closed_round INTEGER, evidence TEXT, kind TEXT
);
CREATE TABLE IF NOT EXISTS round_stats (
  round INTEGER PRIMARY KEY,
  new_errors INTEGER, open_opinions INTEGER, changed_blocks INTEGER,
  novelty REAL, low_novelty INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  initiator TEXT, round INTEGER, run_type TEXT, purpose_items TEXT,
  code TEXT, code_hash TEXT, output TEXT, status TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS live_calls (
  call_key TEXT PRIMARY KEY,
  role TEXT, attempt INTEGER, of_total INTEGER, started_ts TEXT,
  think_chars INTEGER, out_chars INTEGER, last_ts TEXT, think_tail TEXT, problem TEXT, data_ts TEXT,
  out_tail TEXT, note TEXT
);
"""
# live_calls：正在进行的调用（供窗口版左栏头像、实时思考框、直接打断弹窗、解说者用）。
# 不是账本，不进哈希链。2.11 起每次调用（调用键）各占一条，调用结束就删掉自己那一条（见 clear_live_call）；
# 2.10 及以前按角色只记一条——同一角色同时有几次调用时，先结束的会把记录删掉，界面就误显示这个角色空闲。


def code_hashes() -> dict:
    """2.5：程序自身所有文件（.py/.html/.json 等）的哈希，建项目时写进账本——
    同一个版本号对应唯一的内容；别人拿到账本能核对"你跑的到底是哪份代码"。
    返回 {相对路径: 哈希, "__total__": 总哈希}（总哈希＝按路径排序后把"路径:哈希"逐行拼起来再哈希）。"""
    base = Path(__file__).resolve().parent.parent  # 程序包根目录（含 alreadythere/、entry.py、.bat、tools/、examples/）
    out = {}
    try:
        for f in sorted(base.rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts or f.suffix in (".pyc", ".db"):
                continue
            rel = f.relative_to(base)
            if rel.parts[0] in ("tests", "项目", "projects", "data") or rel.name.startswith("."):
                continue
            out[rel.as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()  # 按原始字节算，不受换行转换影响以外的任何解读
    except OSError:
        pass
    out["__total__"] = sha256_text("\n".join(f"{k}:{v}" for k, v in sorted(out.items())))
    return out


class Project:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.db_path = self.root / "project.db"
        self.archive_dir = self.root / "archive"
        self.roles_dir = self.root / "roles"
        self.conn = None
        # 2.0 并行：好几个角色的线程会共用这一个 Project／同一个数据库连接，
        # 用一把可重入锁保证任何时候只有一段代码真的在碰数据库，不会写坏、也不会撞车。
        self.lock = threading.RLock()

    # ---------- 生命周期 ----------
    @classmethod
    def create(cls, root, config: dict) -> "Project":
        p = cls(root)
        if p.db_path.exists():
            raise FileExistsError(f"项目已存在：{p.root}")
        p.root.mkdir(parents=True, exist_ok=True)
        p.archive_dir.mkdir(exist_ok=True)
        p.roles_dir.mkdir(exist_ok=True)
        (p.root / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        p.open()
        from . import __version__
        files = code_hashes()
        p.log("程序", "项目创建", content={"config_hash": sha256_text(canonical_json(config)),
                                          "版本": __version__, "代码总哈希": files.pop("__total__"),
                                          "代码哈希": files})
        return p

    def open(self) -> "Project":
        # check_same_thread=False：2.0 起会有好几个角色的线程共用同一个连接；
        # 真正的安全靠上面那把锁保证一次只有一边在操作，不是靠这个开关本身。
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        return self

    MIGRATIONS = {  # 1.4 新增的列；旧项目打开时自动补上（只加列，不改已有数据）
        "reviews": ["eq_check TEXT", "indep_check TEXT", "indep_source TEXT", "indep_dates TEXT"],
        "opinions": ["content_isolated TEXT"],
        "items": ["ganzhi TEXT"],
        "calls": ["reasoning TEXT", "duration REAL", "max_gap REAL", "first_byte REAL", "think_secs REAL",
                  "served_model TEXT"],
        "live_calls": ["think_chars INTEGER", "out_chars INTEGER", "last_ts TEXT",
                       "think_tail TEXT", "problem TEXT", "data_ts TEXT", "out_tail TEXT", "note TEXT"],
    }

    def _migrate(self):
        # 2.11：旧项目的 live_calls 以角色为主键——这张表不是账本、只存"此刻正在进行的调用"，直接按新结构重建
        pk = [r[1] for r in self.conn.execute("PRAGMA table_info(live_calls)") if r[5]]
        if pk == ["role"]:
            self.conn.execute("DROP TABLE live_calls")
            self.conn.executescript(SCHEMA)
        for table, cols in self.MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for c in cols:
                if c.split()[0] not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {c}")

    @property
    def config(self) -> dict:
        return json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    # ---------- 编号 ----------
    def next_id(self, table: str, prefix: str, col: str) -> str:
        with self.lock:
            n = self.conn.execute(
                f"SELECT MAX(CAST(SUBSTR({col}, {len(prefix) + 1}) AS INTEGER)) FROM {table} "
                f"WHERE {col} LIKE ?", (prefix + "%",)).fetchone()[0] or 0
            return f"{prefix}{n + 1:05d}"

    # ---------- 账本 ----------
    def log(self, actor: str, etype: str, *, round_=None, phase=None,
            ref_type=None, ref_id=None, content=None) -> str:
        """追加一条事件；返回事件编号。哈希覆盖除 hash 以外的全部字段。
        2.0 并行：整个"读上一条哈希 -> 算新哈希 -> 插入"必须锁在一起，不然两个角色的线程
        可能读到同一个"上一条"，链就断了（两条都说自己接在同一条后面）。"""
        with self.lock:
            cur = self.conn.execute("SELECT hash, seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = cur["hash"] if cur else GENESIS
            seq = (cur["seq"] + 1) if cur else 1
            event_id = f"E{seq:06d}"
            body = {
                "seq": seq, "event_id": event_id, "ts": now_iso(), "prev_hash": prev,
                "round": round_, "phase": phase, "actor": actor, "type": etype,
                "ref_type": ref_type, "ref_id": ref_id,
                "content": canonical_json(content) if content is not None else None,
            }
            h = sha256_text(canonical_json(body))
            self.conn.execute(
                "INSERT INTO events (seq,event_id,ts,prev_hash,hash,round,phase,actor,type,ref_type,ref_id,content)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (seq, event_id, body["ts"], prev, h, round_, phase, actor, etype, ref_type, ref_id, body["content"]))
            self.conn.commit()
            return event_id

    def verify_chain(self):
        """逐条重算哈希链；返回 (是否完整, 第一个不一致的事件编号或 None, 事件总数)。"""
        prev = GENESIS
        with self.lock:  # 2.1：读事件表也要加锁，理由同 read_archive
            rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        for r in rows:
            body = {k: r[k] for k in ("seq", "event_id", "ts", "prev_hash", "round", "phase",
                                      "actor", "type", "ref_type", "ref_id", "content")}
            if r["prev_hash"] != prev or sha256_text(canonical_json(body)) != r["hash"]:
                return False, r["event_id"], len(rows)
            prev = r["hash"]
        return True, None, len(rows)

    def verify_files(self):
        """重算检索存档与产物文件的哈希，返回不一致的文件列表。"""
        bad = []
        for r in self.q("SELECT archive_path, content_hash FROM retrievals"):
            f = self.path_of(r["archive_path"])
            if not f.exists() or sha256_text(f.read_text(encoding="utf-8")) != r["content_hash"]:
                bad.append(r["archive_path"])
        for r in self.q("SELECT filename, output_hash FROM artifacts"):
            f = self.path_of(r["filename"])
            if not f.exists() or sha256_text(f.read_text(encoding="utf-8")) != r["output_hash"]:
                bad.append(r["filename"])
        return bad

    # ---------- 文件 ----------
    def save_artifact_file(self, role: str, round_: int, kind: str, text: str) -> tuple[str, str]:
        """按 9.6 程序命名保存产物文件；返回 (相对路径, 哈希)。"""
        h = sha256_text(text)
        d = self.roles_dir / role
        d.mkdir(parents=True, exist_ok=True)
        name = f"{self.config.get('project_id', 'P')}_R{round_:02d}_{role}_{kind}_{h[:8]}.json"
        (d / name).write_text(text, encoding="utf-8")
        return (d / name).relative_to(self.root).as_posix(), h

    def archive_text(self, text: str) -> tuple[str, str]:
        h = sha256_text(text)
        path = self.archive_dir / f"{h}.txt"
        if not path.exists():  # 相同内容按哈希只存一份（9.8）
            path.write_text(text, encoding="utf-8")
        return path.relative_to(self.root).as_posix(), h

    def path_of(self, rel: str) -> Path:
        """账本里记的相对路径 -> 本机路径（1.4：Windows 上建的项目在别的系统上也能读，反斜杠统一换成 /）。"""
        return self.root / str(rel).replace("\\", "/")

    def read_archive(self, retrieval_id: str) -> str | None:
        with self.lock:  # 2.1：原来这里没加锁，并行时偶发 sqlite3.InterfaceError
            r = self.conn.execute("SELECT archive_path FROM retrievals WHERE retrieval_id=?",
                                  (retrieval_id,)).fetchone()
        if not r:
            return None
        return self.path_of(r["archive_path"]).read_text(encoding="utf-8-sig")

    # ---------- 便捷查询 ----------
    def q(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def exec(self, sql: str, args=()):
        with self.lock:
            self.conn.execute(sql, args)
            self.conn.commit()

    @contextmanager
    def batch(self):
        """2.0 并行：把"一个角色自己的一串本地写入"锁在一起，保证这一串记录连着出现、
        不会被另一个角色的线程插进来打断（角色之间谁先谁后仍看谁先做完，但各自内部不会被打散）。
        用法：with project.batch(): ...里面正常调用 log/exec 等方法（可重入锁，不会卡死）。"""
        with self.lock:
            yield

    # ---------- 实时调用状态（不进账本）----------
    # 2.11：以调用键为主键，每次调用各一条。同一调用键的重试（第几次尝试）沿用同一条。
    def set_live_call(self, role: str, call_key: str, attempt: int, of_total: int):
        """记录一次正在进行的调用尝试（窗口版左栏头像、实时思考框用）。重试时问题标记保留，直到收到新的真实内容。"""
        with self.lock:
            self.conn.execute(
                "INSERT INTO live_calls (call_key,role,attempt,of_total,started_ts) VALUES (?,?,?,?,?) "
                "ON CONFLICT(call_key) DO UPDATE SET role=excluded.role, attempt=excluded.attempt, "
                "of_total=excluded.of_total, started_ts=excluded.started_ts, think_chars=0, out_chars=0, last_ts=NULL, "
                "think_tail='', out_tail='', data_ts=NULL",
                (call_key, role, attempt, of_total, now_iso()))
            self.conn.commit()

    THINK_TAIL_MAX = 40000  # 2.5：实时思考只留最近这么多字（完整内容调用结束后进账本 calls.reasoning）

    def set_live_progress(self, call_key: str, think_chars: int, out_chars: int, think_tail: str | None = None,
                          has_data: bool = True, out_tail: str | None = None):
        """last_ts＝最近一次收到任何东西（含保活）；data_ts＝最近一次收到真正的内容。
        2.5：只要还在收（含保活）就是工作中；收到真实内容后清掉上一次尝试的故障标记。
        2.11：另存回答的最近一段（out_tail），用户停止或直接打断时连同思考一起存进账本。"""
        with self.lock:
            now = now_iso()
            if has_data:
                self.conn.execute("UPDATE live_calls SET think_chars=?, out_chars=?, last_ts=?, data_ts=?, "
                                  "think_tail=COALESCE(?, think_tail), out_tail=COALESCE(?, out_tail), problem=NULL "
                                  "WHERE call_key=?",
                                  (think_chars, out_chars, now, now,
                                   think_tail[-self.THINK_TAIL_MAX:] if think_tail is not None else None,
                                   out_tail[-self.THINK_TAIL_MAX:] if out_tail is not None else None, call_key))
            else:
                self.conn.execute("UPDATE live_calls SET last_ts=? WHERE call_key=?", (now, call_key))
            self.conn.commit()

    def set_live_problem(self, call_key: str, reason: str):
        """2.5：这次尝试确认出了问题（断连／网络错误／可重试的服务端错误）——头像才显示"响应中"。"""
        with self.lock:
            self.conn.execute("UPDATE live_calls SET problem=? WHERE call_key=?", (reason[:300], call_key))
            self.conn.commit()

    def set_live_note(self, call_key: str, note: str | None):
        """2.11：附加说明（例如"疑似循环（未掐断）""正在续写"），只显示，不改变头像状态。"""
        with self.lock:
            self.conn.execute("UPDATE live_calls SET note=? WHERE call_key=?", ((note or None) and note[:300], call_key))
            self.conn.commit()

    def live_thinking(self, call_key: str) -> str:
        with self.lock:
            r = self.conn.execute("SELECT think_tail FROM live_calls WHERE call_key=?", (call_key,)).fetchone()
        return (r["think_tail"] or "") if r else ""

    def clear_live_call(self, call_key: str):
        with self.lock:
            self.conn.execute("DELETE FROM live_calls WHERE call_key=?", (call_key,))
            self.conn.commit()

    def save_unfinished(self, reason: str) -> int:
        """2.11：用户停止运行或直接打断时，把还在进行的调用已收到的内容存进账本（每次调用一条"未完成调用"事件），
        然后清空 live_calls。只供事后查看，不参与审查；之后继续运行时这次调用照常重新发出。
        存的是界面实时用的那一段：思考和回答各最近 4 万字（字数是全部已收到的字数）。返回存了几条。"""
        import datetime as _dt
        rows = self.q("SELECT * FROM live_calls ORDER BY started_ts")
        now = _dt.datetime.now(_dt.timezone.utc)
        for r in rows:
            try:
                secs = int((now - _dt.datetime.fromisoformat(r["started_ts"])).total_seconds())
            except (TypeError, ValueError):
                secs = None
            tc, oc = r["think_chars"] or 0, r["out_chars"] or 0
            tt, ot = r["think_tail"] or "", r["out_tail"] or ""
            self.log(r["role"] or "程序", "未完成调用", ref_type="call_key", ref_id=r["call_key"],
                     content={"原因": reason, "role": r["role"], "call_key": r["call_key"], "第几次尝试": r["attempt"],
                              "开始时间": r["started_ts"], "已用秒数": secs, "思考字数": tc, "回答字数": oc,
                              "思考（最近一段）": tt, "回答（最近一段）": ot,
                              "说明": ("只存了最近一段（全部已收到的字数见上）" if len(tt) < tc or len(ot) < oc else "已收到的全部内容")
                              + f"；这次调用没有完成（{reason}）；不参与审查",
                              **({"问题": r["problem"]} if r["problem"] else {}),
                              **({"附注": r["note"]} if r["note"] else {})})
        self.exec("DELETE FROM live_calls")
        return len(rows)
