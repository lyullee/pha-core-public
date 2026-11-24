"""SQLite append-only baselines, revision checks, run lineage and managed evidence."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .domain import Evidence, Record, StudySnapshot, canonical, digest


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.path.parent / (self.path.stem + "_artifacts")
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO schema_version VALUES(1);
            CREATE TABLE IF NOT EXISTS baselines(
                digest TEXT PRIMARY KEY, project_id TEXT NOT NULL, study_id TEXT NOT NULL,
                baseline_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(project_id, baseline_id, revision));
            CREATE TABLE IF NOT EXISTS revisions(
                project_id TEXT NOT NULL, entity_id TEXT NOT NULL, revision INTEGER NOT NULL,
                digest TEXT NOT NULL, PRIMARY KEY(project_id, entity_id, revision));
            CREATE TABLE IF NOT EXISTS heads(
                project_id TEXT NOT NULL, study_id TEXT NOT NULL,
                digest TEXT NOT NULL REFERENCES baselines(digest), PRIMARY KEY(project_id, study_id));
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, baseline_digest TEXT NOT NULL REFERENCES baselines(digest),
                module TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
                dependency_run_ids TEXT NOT NULL, code_digest TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence(
                sha256 TEXT PRIMARY KEY, original_name TEXT NOT NULL, managed_path TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS baselines_no_update BEFORE UPDATE ON baselines BEGIN SELECT RAISE(ABORT, 'immutable baseline'); END;
            CREATE TRIGGER IF NOT EXISTS baselines_no_delete BEFORE DELETE ON baselines BEGIN SELECT RAISE(ABORT, 'immutable baseline'); END;
            CREATE TRIGGER IF NOT EXISTS revisions_no_update BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT, 'immutable revision'); END;
            CREATE TRIGGER IF NOT EXISTS runs_completed_no_update BEFORE UPDATE ON runs WHEN OLD.status!='RUNNING' BEGIN SELECT RAISE(ABORT, 'immutable completed run'); END;
            CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'run history must be retained'); END;
            """)
            if db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] != 1:
                raise ValueError("unsupported database schema")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def save(self, snapshot: StudySnapshot) -> str:
        value = snapshot.digest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_evidence(db, snapshot)
            if db.execute("SELECT 1 FROM baselines WHERE digest=?", (value,)).fetchone():
                return value
            head = db.execute("SELECT b.payload FROM heads h JOIN baselines b ON h.digest=b.digest WHERE h.project_id=? AND h.study_id=?",
                              (snapshot.project_id, snapshot.study_id)).fetchone()
            if head:
                prior = json.loads(head[0])
                if prior["baseline_id"] == snapshot.baseline_id and snapshot.revision <= prior["revision"]:
                    raise ValueError("baseline changes require a greater revision")
            for name in type(snapshot).model_fields:
                records = getattr(snapshot, name)
                if not isinstance(records, tuple):
                    continue
                records = records + tuple(b for sif in records if hasattr(sif, "blocks") for b in sif.blocks)
                for record in records:
                    if not isinstance(record, (Record, Evidence)):
                        continue
                    key = (snapshot.project_id, record.id, record.revision)
                    found = db.execute("SELECT digest FROM revisions WHERE project_id=? AND entity_id=? AND revision=?", key).fetchone()
                    if found and found[0] != record.digest():
                        raise ValueError(f"{record.id}: content changed without entity revision increment")
                    newest = db.execute("SELECT MAX(revision) FROM revisions WHERE project_id=? AND entity_id=?", key[:2]).fetchone()[0]
                    if newest is not None and record.revision < newest:
                        raise ValueError(f"{record.id}: cannot silently reactivate an older entity revision")
                    db.execute("INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)", (*key, record.digest()))
            db.execute("INSERT INTO baselines VALUES(?,?,?,?,?,?,?)", (value, snapshot.project_id, snapshot.study_id,
                       snapshot.baseline_id, snapshot.revision, snapshot.model_dump_json(), now()))
            db.execute("INSERT INTO heads VALUES(?,?,?) ON CONFLICT(project_id,study_id) DO UPDATE SET digest=excluded.digest",
                       (snapshot.project_id, snapshot.study_id, value))
        return value

    def load(self, baseline_digest: str) -> StudySnapshot:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM baselines WHERE digest=?", (baseline_digest,)).fetchone()
        if not row:
            raise KeyError(baseline_digest)
        result = StudySnapshot.model_validate_json(row[0])
        if result.digest() != baseline_digest:
            raise ValueError("baseline hash mismatch")
        with self.connection() as db:
            self._check_evidence(db, result)
        return result

    def _check_evidence(self, db, snapshot):
        for e in snapshot.evidence:
            row = db.execute("SELECT managed_path FROM evidence WHERE sha256=?", (e.sha256,)).fetchone()
            if snapshot.mode == "reviewed" and not row:
                raise ValueError(f"{e.id}: ingest reviewed evidence before saving/running")
            if row:
                path = Path(row[0])
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != e.sha256:
                    raise ValueError("managed evidence hash mismatch or missing file")

    def list_baselines(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT digest,project_id,study_id,baseline_id,revision,created_at FROM baselines ORDER BY created_at DESC")]

    def ingest_evidence(self, source):
        source = Path(source)
        content = source.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        target = self.artifacts / "evidence" / sha
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != content:
            raise ValueError("managed evidence collision/corruption")
        if not target.exists():
            target.write_bytes(content)
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?)", (sha, source.name, str(target)))
        return {"sha256": sha, "original_name": source.name, "managed_path": str(target)}

    def start_run(self, baseline_digest, module, dependencies=()):
        self.load(baseline_digest)
        run_id = str(uuid4())
        with self.connection() as db:
            for ref in dependencies:
                found = db.execute("SELECT baseline_digest,status FROM runs WHERE id=?", (ref,)).fetchone()
                if not found or found[0] != baseline_digest or found[1] != "SUCCEEDED":
                    raise ValueError("dependency run must succeed on the same baseline")
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)", (run_id, baseline_digest, module,
                       "RUNNING", None, None, canonical(list(dependencies)), code_digest(), now()))
        return run_id

    def finish_run(self, run_id, *, result=None, error=None):
        with self.connection() as db:
            cursor = db.execute("UPDATE runs SET status=?,result=?,error=? WHERE id=? AND status='RUNNING'",
                                ("FAILED" if error else "SUCCEEDED", canonical(result) if result is not None else None, error, run_id))
            if cursor.rowcount != 1:
                raise ValueError("completed runs are immutable or run does not exist")

    def run(self, run_id):
        with self.connection() as db:
            row = db.execute("SELECT r.*,b.project_id,b.study_id,h.digest AS head_digest FROM runs r JOIN baselines b ON b.digest=r.baseline_digest LEFT JOIN heads h ON h.project_id=b.project_id AND h.study_id=b.study_id WHERE r.id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        result = dict(row)
        result["dependency_run_ids"] = json.loads(result["dependency_run_ids"])
        if result["result"] is not None:
            result["result"] = json.loads(result["result"])
        result["stale"] = result["baseline_digest"] != result.pop("head_digest") or result["code_digest"] != source_code_digest()
        return result


def now():
    return datetime.now(timezone.utc).isoformat()


def source_code_digest():
    root = Path(__file__).parent
    return digest({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.py"))})


_LOADED_CODE_DIGEST = source_code_digest()


def code_digest():
    if source_code_digest() != _LOADED_CODE_DIGEST:
        raise ValueError("source files changed after process startup; restart before running calculations")
    return _LOADED_CODE_DIGEST
