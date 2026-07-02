#!/usr/bin/env python3
"""DGX1 Coordinator - SQLite-backed HTTP lease queue."""
import os, json, sqlite3, time, hashlib, uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

DB_PATH = os.environ.get("COORDINATOR_DB", "/home/steve/coordinator.db")
QUEUE_DIR = Path(os.environ.get("WORK_QUEUE", "/home/steve/work-queue"))
DONE_DIR = Path(os.environ.get("WORK_DONE", "/home/steve/work-done"))
FAILED_DIR = Path(os.environ.get("WORK_FAILED", "/home/steve/work-failed"))
LEASE_TIMEOUT = int(os.environ.get("LEASE_TIMEOUT", "600"))
MAX_IN_FLIGHT = int(os.environ.get("MAX_IN_FLIGHT", "2"))

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            worker_id TEXT,
            leased_at TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def scan_queue():
    """Add new JSONL files from queue directory to database."""
    conn = sqlite3.connect(DB_PATH)
    for f in QUEUE_DIR.glob("*.jsonl"):
        pid = f.stem
        exists = conn.execute("SELECT 1 FROM tasks WHERE id=?", (pid,)).fetchone()
        if not exists:
            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            conn.execute(
                "INSERT INTO tasks (id, path, sha256, status) VALUES (?, ?, ?, 'pending')",
                (pid, str(f), sha)
            )
    conn.commit()
    conn.close()

def reclaim_expired():
    """Re-queue tasks with expired leases."""
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=LEASE_TIMEOUT)).isoformat()
    conn.execute(
        "UPDATE tasks SET status='pending', worker_id=NULL, leased_at=NULL, retry_count=retry_count+1 WHERE status='leased' AND leased_at < ? AND retry_count < 3",
        (cutoff,)
    )
    conn.execute(
        "UPDATE tasks SET status='failed' WHERE status='leased' AND leased_at < ? AND retry_count >= 3",
        (cutoff,)
    )
    conn.commit()
    conn.close()

def get_pending(worker_id, capacity):
    conn = sqlite3.connect(DB_PATH)
    # Check in-flight for this worker (excluding this worker's own leases - allow reclaim)
    in_flight = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='leased' AND worker_id != ?",
        (worker_id,)
    ).fetchone()[0]
    if in_flight >= MAX_IN_FLIGHT:
        conn.close()
        return None, "max_in_flight"
    
    # Get oldest pending tasks, or this worker's own leased tasks (reclaim)
    rows = conn.execute(
        "SELECT id, path, sha256, retry_count FROM tasks WHERE (status='pending' OR (status='leased' AND worker_id=?)) ORDER BY created_at LIMIT ?",
        (worker_id, capacity)
    ).fetchall()
    
    if not rows:
        conn.close()
        return None, "empty"
    
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for row in rows:
        pid, path, sha, retries = row
        conn.execute(
            "UPDATE tasks SET status='leased', worker_id=?, leased_at=? WHERE id=?",
            (worker_id, now, pid)
        )
        out.append({
            "package_id": pid,
            "payload_path": path,
            "sha256": sha,
            "retry_count": retries,
        })
    conn.commit()
    conn.close()
    return out, None

def submit_result(pid, status):
    conn = sqlite3.connect(DB_PATH)
    if status == "done":
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (pid,))
        # Move file to done directory
        row = conn.execute("SELECT path FROM tasks WHERE id=?", (pid,)).fetchone()
        if row:
            src = Path(row[0])
            if src.exists():
                dst = DONE_DIR / f"{pid}.jsonl"
                src.rename(dst)
                conn.execute("UPDATE tasks SET path=? WHERE id=?", (str(dst), pid))
    elif status == "failed":
        conn.execute(
            "UPDATE tasks SET status='pending', worker_id=NULL, leased_at=NULL, retry_count=retry_count+1 WHERE id=?",
            (pid,)
        )
    conn.commit()
    conn.close()

def heartbeat(pid):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE tasks SET leased_at=? WHERE id=? AND status='leased'", (now, pid))
    conn.commit()
    conn.close()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    
    def _json(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
    
    def do_GET(self):
        if self.path == "/v1/health":
            conn = sqlite3.connect(DB_PATH)
            queue_depth = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
            in_flight = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='leased'").fetchone()[0]
            done_count = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
            conn.close()
            self._json(200, {
                "status": "ok",
                "queue_depth": queue_depth,
                "in_flight": in_flight,
                "done": done_count,
                "db_ok": True,
            })
        elif self.path.startswith("/v1/work/download/"):
            pid = self.path.split("/")[-1]
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT path FROM tasks WHERE id=?", (pid,)).fetchone()
            conn.close()
            if row and Path(row[0]).exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                self.wfile.write(Path(row[0]).read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        
        if self.path == "/v1/work/request":
            scan_queue()
            reclaim_expired()
            worker_id = req.get("worker_id", "unknown")
            capacity = min(req.get("capacity", 1), int(os.environ.get("MAX_BATCH", "100")))
            packages, err = get_pending(worker_id, capacity)
            if err == "max_in_flight":
                self._json(429, {"error": "max_in_flight reached"})
            elif err == "empty":
                self._json(204, {})
            else:
                self._json(200, {"packages": packages})
        elif self.path == "/v1/work/result":
            pid = req.get("package_id")
            status = req.get("status")
            submit_result(pid, status)
            self._json(200, {"ok": True})
        elif self.path == "/v1/work/heartbeat":
            pid = req.get("package_id")
            heartbeat(pid)
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

if __name__ == "__main__":
    for d in (QUEUE_DIR, DONE_DIR, FAILED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    init_db()
    print(f"SQLite Coordinator on 0.0.0.0:9000 (db={DB_PATH})")
    HTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
