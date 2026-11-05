"""Versioned project files and durable workbench metadata.

Novel text remains in the original TXT files. SQLite holds only project registration,
history, jobs, and finalization receipts. All local writers share one reentrant lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def revision(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class Conflict(ValueError):
    pass


class Store:
    STATIC = {
        "architecture": "Novel_architecture.txt", "directory": "Novel_directory.txt",
        "summary": "global_summary.txt", "characters": "character_state.txt",
        "plot_arcs": "plot_arcs.txt",
    }
    PER_CHAPTER = {"chapter": "chapters", "prompt": "prompts", "review": "reviews"}
    DEFAULTS = {"topic": "", "genre": "玄幻", "num_chapters": 10,
                "word_number": 3000, "user_guidance": "", "filepath": "", "chapter_num": 1,
                "characters_involved": "", "key_items": "", "scene_location": "", "time_constraint": ""}

    def __init__(self, root: Path, data_dir: Path | None = None):
        self.root = Path(root).resolve()
        self.workspace = self.root.parent
        self.data_dir = Path(data_dir or self.root / "backend" / "runtime").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "workbench.sqlite3"
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT UNIQUE NOT NULL,
                  params TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
                  chapter INTEGER NOT NULL, content TEXT NOT NULL, revision TEXT NOT NULL,
                  source TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS version_lookup ON versions(project_id, kind, chapter);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS finalizations (
                  project_id TEXT NOT NULL, chapter INTEGER NOT NULL, revision TEXT NOT NULL,
                  details TEXT NOT NULL, PRIMARY KEY(project_id, chapter));
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def locked(self, project=None):
        with self.lock:
            yield

    @staticmethod
    def _project(row):
        return {"id": row["id"], "name": row["name"], "path": row["path"],
                "params": json.loads(row["params"]), "created_at": row["created_at"]}

    def projects(self):
        with self.lock, self.db() as db:
            return [self._project(r) for r in db.execute("SELECT * FROM projects ORDER BY created_at")]

    def project(self, project_id):
        with self.lock, self.db() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise FileNotFoundError("未找到这部小说，请重新选择。")
        return self._project(row)

    def validate_params(self, params):
        merged = dict(self.DEFAULTS)
        for key in merged:
            if key in params:
                merged[key] = params[key]
        for key, low, high in (("num_chapters", 1, 10000), ("word_number", 100, 100000)):
            try:
                value = int(merged[key])
            except (ValueError, TypeError):
                raise ValueError(f"{key} 必须为整数。") from None
            if not low <= value <= high:
                raise ValueError(f"{key} 必须在 {low}–{high} 之间。")
            merged[key] = value
        for key in ("topic", "genre", "user_guidance", "filepath", "characters_involved", "key_items", "scene_location", "time_constraint"):
            merged[key] = str(merged[key] or "")[:100000]
        try:
            merged["chapter_num"] = int(merged["chapter_num"])
        except (ValueError, TypeError):
            raise ValueError("默认章号必须为整数。") from None
        if not 1 <= merged["chapter_num"] <= 10000:
            raise ValueError("默认章号必须在 1–10000 之间。")
        return merged

    def register(self, name, path=None, params=None, existing=False):
        name = str(name or "未命名小说").strip()[:100]
        if path:
            dest = Path(path).expanduser()
            if not dest.is_absolute():
                dest = self.workspace / dest
        else:
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "novel"
            dest = self.workspace / "小说作品" / f"{safe_name}-{uuid.uuid4().hex[:6]}"
        dest = dest.resolve()
        if dest == self.workspace or dest == self.root or dest.is_relative_to(self.root):
            raise ValueError("请选择独立的小说目录，不能使用程序代码目录。")
        if existing and not dest.is_dir():
            raise ValueError("要打开的小说目录不存在。")
        with self.lock:
            with self.db() as db:
                row = db.execute("SELECT * FROM projects WHERE path=?", (str(dest),)).fetchone()
                if row:
                    return self._project(row)
            dest.mkdir(parents=True, exist_ok=True)
            values = self.validate_params(params or {})
            project_id = uuid.uuid4().hex
            with self.db() as db:
                db.execute("INSERT INTO projects VALUES (?,?,?,?,?)",
                           (project_id, name, str(dest), json.dumps(values, ensure_ascii=False), now()))
            return self.project(project_id)

    def update_project(self, project_id, params):
        project = self.project(project_id)
        values = self.validate_params({**project["params"], **params})
        with self.lock, self.db() as db:
            db.execute("UPDATE projects SET params=? WHERE id=?",
                       (json.dumps(values, ensure_ascii=False), project_id))
        return self.project(project_id)

    def document_path(self, project, kind, chapter=1):
        if kind in self.STATIC:
            relative = self.STATIC[kind]
        elif kind in self.PER_CHAPTER:
            try:
                chapter = int(chapter)
            except (ValueError, TypeError):
                raise ValueError("章号必须为正整数。") from None
            if not 1 <= chapter <= 10000:
                raise ValueError("章号必须在 1–10000 之间。")
            relative = f"{self.PER_CHAPTER[kind]}/chapter_{chapter}.txt"
        else:
            raise ValueError("不支持的文档类型。")
        base = Path(project["path"]).resolve()
        path = (base / relative).resolve()
        if not path.is_relative_to(base):
            raise ValueError("文件位置超出小说目录。")
        return path

    def doc(self, project, kind, chapter=1):
        with self.lock:
            path = self.document_path(project, kind, chapter)
            content = path.read_text(encoding="utf-8-sig") if path.exists() else ""
            return {"kind": kind, "chapter": int(chapter), "content": content,
                    "revision": revision(content), "filename": str(path)}

    @staticmethod
    def atomic_write(path, content):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def apply_documents(self, project, documents, source="manual"):
        """Check the entire write set before replacing any files; roll back on error."""
        with self.lock:
            prepared, seen = [], set()
            for item in documents:
                kind, chapter = item["kind"], int(item.get("chapter", 1))
                key = (kind, chapter if kind in self.PER_CHAPTER else 1)
                if key in seen:
                    raise ValueError("同一提交中出现重复文档。")
                seen.add(key)
                current = self.doc(project, kind, chapter)
                expected = item.get("base_revision")
                if expected is not None and current["revision"] != expected:
                    raise Conflict(f"{kind} 已有更新，请重新载入或比较历史后再保存。")
                content = item["content"]
                if not isinstance(content, str) or len(content) > 5_000_000:
                    raise ValueError("文档必须为文本，且不能超过 500 万字符。")
                path = self.document_path(project, kind, chapter)
                prepared.append((item, current, path, path.exists()))
            written = []
            try:
                with self.db() as db:
                    for item, current, path, existed in prepared:
                        chapter = current["chapter"] if current["kind"] in self.PER_CHAPTER else 1
                        if current["content"] and not db.execute(
                            "SELECT 1 FROM versions WHERE project_id=? AND kind=? AND chapter=? AND revision=?",
                            (project["id"], item["kind"], chapter, current["revision"]),
                        ).fetchone():
                            db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)", (
                                uuid.uuid4().hex, project["id"], item["kind"], chapter,
                                current["content"], current["revision"], "原有文件", now()))
                        self.atomic_write(path, item["content"])
                        written.append((path, current["content"], existed))
                        db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)", (
                            uuid.uuid4().hex, project["id"], item["kind"], chapter,
                            item["content"], revision(item["content"]), source, now()))
            except Exception:
                for path, content, existed in reversed(written):
                    if existed:
                        self.atomic_write(path, content)
                    else:
                        path.unlink(missing_ok=True)
                raise
            return [self.doc(project, item["kind"], item.get("chapter", 1)) for item in documents]

    def save_doc(self, project, kind, content, revision=None, chapter=1, source="manual"):
        return self.apply_documents(project, [{"kind": kind, "chapter": chapter,
            "content": content, "base_revision": revision}], source)[0]

    def history(self, project, kind, chapter=1):
        self.document_path(project, kind, chapter)
        chapter = int(chapter) if kind in self.PER_CHAPTER else 1
        with self.lock, self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,content,revision,source,created_at FROM versions "
                "WHERE project_id=? AND kind=? AND chapter=? ORDER BY created_at DESC LIMIT 100",
                (project["id"], kind, chapter))]

    def roles(self, project):
        base = Path(project["path"]) / "角色库"
        result = []
        if base.is_dir():
            for path in sorted(base.glob("*/*.txt")):
                if not path.resolve().is_relative_to(Path(project["path"]).resolve()):
                    continue
                result.append({"id": path.relative_to(base).as_posix(), "name": path.stem,
                    "category": path.parent.name, "content": path.read_text(encoding="utf-8-sig")})
        return result

    def finalizations(self, project):
        with self.lock, self.db() as db:
            return [{"chapter": r["chapter"], "revision": r["revision"],
                     "details": json.loads(r["details"])} for r in db.execute(
                "SELECT * FROM finalizations WHERE project_id=? ORDER BY chapter", (project["id"],))]

    def get_finalization(self, project, chapter):
        return next((r for r in self.finalizations(project) if r["chapter"] == int(chapter)), None)

    def record_finalization(self, project, chapter, revision, details):
        with self.lock, self.db() as db:
            db.execute("INSERT OR REPLACE INTO finalizations VALUES (?,?,?,?)", (
                project["id"], int(chapter), revision, json.dumps(details, ensure_ascii=False)))

    def save_job(self, job):
        with self.lock, self.db() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?)",
                       (job["id"], json.dumps(job, ensure_ascii=False)))

    def jobs(self):
        with self.lock, self.db() as db:
            return [json.loads(r["data"]) for r in db.execute("SELECT data FROM jobs")]

    def chapters(self, project):
        from chapter_directory_parser import parse_chapter_blueprint
        parsed = parse_chapter_blueprint(self.doc(project, "directory")["content"])
        by_number = {r["chapter_number"]: r for r in parsed}
        chapter_dir = Path(project["path"]) / "chapters"
        for path in chapter_dir.glob("chapter_*.txt"):
            match = re.fullmatch(r"chapter_(\d+)\.txt", path.name)
            if match and 1 <= int(match[1]) <= 10000:
                n = int(match[1])
                by_number.setdefault(n, {"chapter_number": n, "chapter_title": f"第{n}章", "chapter_summary": ""})
        result, warnings = [], []
        records = {r["chapter"]: r for r in self.finalizations(project)}
        stale_after = None
        for n, row in sorted(by_number.items()):
            doc = self.doc(project, "chapter", n)
            record = records.get(n)
            status = "draft" if doc["content"].strip() else "empty"
            if record:
                if record["revision"] != doc["revision"]:
                    status, stale_after = "modified", n if stale_after is None else stale_after
                elif stale_after is not None or record["details"].get("memory") == "stale":
                    status = "memory_stale"
                else:
                    details = record["details"]
                    index_status = details.get("index", details.get("vector", ""))
                    status = "finalized" if index_status in ("succeeded", "success", "done", True) else "index_pending"
            result.append({**row, "word_count": len(re.sub(r"\s", "", doc["content"])), "status": status})
        numbers = [r["chapter_number"] for r in parsed]
        if len(numbers) != len(set(numbers)):
            warnings.append("目录含有重复章号，请编辑后重新解析。")
        if numbers and sorted(set(numbers)) != list(range(1, max(numbers) + 1)):
            warnings.append("目录章号不连续，请补齐缺章。")
        if stale_after is not None:
            warnings.append(f"第 {stale_after} 章已在定稿后修改，后续创作记忆需重新处理。")
        return {"chapters": result, "warnings": warnings}
