#!/usr/bin/env python3
"""DGX2 Worker - demand-driven work consumer with local AEON vLLM endpoint."""
import os, sys, json, time, hashlib, shutil, signal
from pathlib import Path
import requests

COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "http://192.168.50.1:9000")
AEON_URL = os.environ.get("AEON_URL", "http://localhost:8001/v1")
INBOX = Path(os.environ.get("WORK_INBOX", "/home/steve/work-inbox"))
OUTBOX = Path(os.environ.get("WORK_OUTBOX", "/home/steve/work-outbox"))
ARCHIVE = Path(os.environ.get("WORK_ARCHIVE", "/home/steve/work-archive"))
LEASE_DURATION = int(os.environ.get("LEASE_DURATION", "600"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "60"))
MAX_BATCH = int(os.environ.get("MAX_BATCH", "100"))

for d in (INBOX, OUTBOX, ARCHIVE):
    d.mkdir(parents=True, exist_ok=True)

_shutdown_requested = False

def _handle_sigterm(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    print("Shutdown signal received, finishing current work...")

signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


def fetch_work():
    """Request work from DGX1 coordinator and download packages."""
    capacity = max(1, MAX_BATCH - len(list(INBOX.glob("*.jsonl"))))
    if capacity <= 0:
        return []
    try:
        resp = requests.post(
            f"{COORDINATOR_URL}/v1/work/request",
            json={"worker_id": "dgx2", "capacity": capacity, "lease_duration_sec": LEASE_DURATION},
            timeout=35,
        )
        if resp.status_code == 200:
            packages = resp.json().get("packages", [])
            # Download each package payload to local inbox
            for pkg in packages:
                # Read file from coordinator via HTTP (since payload_path is on DGX1)
                try:
                    file_resp = requests.get(f"{COORDINATOR_URL}/v1/work/download/{pkg['package_id']}", timeout=30)
                    if file_resp.status_code == 200:
                        dst = INBOX / f"{pkg['package_id']}.jsonl"
                        dst.write_bytes(file_resp.content)
                        # Verify checksum
                        data = dst.read_bytes()
                        if hashlib.sha256(data).hexdigest() != pkg["sha256"]:
                            print(f"Checksum mismatch for {pkg['package_id']}")
                            dst.unlink()
                            continue
                    else:
                        print(f"Failed to download {pkg['package_id']}: HTTP {file_resp.status_code}")
                        continue
                except requests.exceptions.RequestException as e:
                    print(f"Download failed for {pkg['package_id']}: {e}")
                    continue
            return packages
        elif resp.status_code == 204:
            return []  # no work available
        elif resp.status_code == 429:
            print("Max in-flight reached, waiting for lease timeout or processing existing work")
            return []
        else:
            print(f"Coordinator error: {resp.status_code}")
            return []
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        return []


def submit_result(package_id, status, result_path):
    """Submit completion to coordinator."""
    try:
        resp = requests.post(
            f"{COORDINATOR_URL}/v1/work/result",
            json={"package_id": package_id, "worker_id": "dgx2", "status": status, "result_path": str(result_path)},
            timeout=10,
        )
        return resp.status_code == 200
    except requests.exceptions.RequestException as e:
        print(f"Result submit failed: {e}")
        return False


def send_heartbeat(package_id):
    """Keep lease alive."""
    try:
        requests.post(
            f"{COORDINATOR_URL}/v1/work/heartbeat",
            json={"package_id": package_id, "worker_id": "dgx2"},
            timeout=5,
        )
    except requests.exceptions.RequestException:
        pass


def process_package(pkg):
    """Process a single JSONL package through AEON vLLM."""
    pid = pkg["package_id"]
    inbox_path = INBOX / f"{pid}.jsonl"
    if not inbox_path.exists():
        print(f"Package {pid} not found in inbox")
        return False

    # Verify checksum
    data = inbox_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != pkg["sha256"]:
        print(f"Package {pid} checksum mismatch!")
        return False

    # Process each line
    result_path = OUTBOX / f"{pid}.done.jsonl"
    last_heartbeat = time.time()

    with open(inbox_path, "r") as fin, open(result_path, "w") as fout:
        for line_num, line in enumerate(fin, 1):
            # Heartbeat
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                send_heartbeat(pid)
                last_heartbeat = time.time()

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                fout.write(json.dumps({"input": line.strip(), "error": "invalid json"}) + "\n")
                continue

            prompt = record.get("prompt", "")
            if not prompt:
                fout.write(json.dumps({"input": record, "error": "no prompt"}) + "\n")
                continue

            # Call AEON vLLM
            try:
                resp = requests.post(
                    f"{AEON_URL}/chat/completions",
                    json={
                        "model": "aeon",
                        "messages": [
                            {"role": "system", "content": "You are a helpful assistant."},
                            {"role": "user", "content": prompt}
                        ],
                        "max_tokens": record.get("max_tokens", 500),
                        "temperature": record.get("temperature", 0.7),
                    },
                    timeout=120,
                )
                if resp.status_code == 200:
                    result = resp.json()
                    msg = result["choices"][0]["message"]
                    # Qwen3 may output reasoning in content field; extract actual response
                    content = msg.get("content") or ""
                    # Strip thinking process if present
                    if content and ("Thinking Process:" in content or "thinking process" in content.lower()):
                        # Find the actual answer after thinking markers
                        lines = content.split("\n")
                        actual_lines = []
                        in_thinking = False
                        for line in lines:
                            if "Thinking Process:" in line or "thinking process" in line.lower():
                                in_thinking = True
                                continue
                            if in_thinking and line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                                # Likely start of actual response
                                in_thinking = False
                            if not in_thinking:
                                actual_lines.append(line)
                        content = "\n".join(actual_lines).strip()
                    # Fallback: if still empty or just thinking, use reasoning field if available
                    if not content or "thinking" in content.lower():
                        content = msg.get("reasoning", "") or content
                    out_record = {
                        "input": record,
                        "output": content,
                        "finish_reason": result["choices"][0].get("finish_reason"),
                        "usage": result.get("usage"),
                    }
                else:
                    out_record = {"input": record, "error": f"HTTP {resp.status_code}", "body": resp.text[:200]}
            except requests.exceptions.RequestException as e:
                out_record = {"input": record, "error": str(e)}

            fout.write(json.dumps(out_record) + "\n")

    # Move to archive
    archive_path = ARCHIVE / f"{pid}.jsonl"
    inbox_path.rename(archive_path)

    # Submit result
    if submit_result(pid, "done", result_path):
        print(f"Completed {pid}")
        return True
    else:
        print(f"Failed to submit result for {pid}")
        return False


def main():
    print(f"DGX2 Worker starting. Coordinator: {COORDINATOR_URL}, AEON: {AEON_URL}")

    while not _shutdown_requested:
        # Check if we need more work
        inbox_count = len(list(INBOX.glob("*.jsonl")))
        if inbox_count == 0:
            print("Inbox empty, fetching work...")
            packages = fetch_work()
            if packages:
                print(f"Received {len(packages)} packages")
            else:
                print("No work available, sleeping 10s...")
                time.sleep(10)
                continue

        # Process available work
        for pkg_file in sorted(INBOX.glob("*.jsonl")):
            if _shutdown_requested:
                break
            pid = pkg_file.stem
            pkg = {
                "package_id": pid,
                "sha256": hashlib.sha256(pkg_file.read_bytes()).hexdigest(),
            }
            success = process_package(pkg)
            if not success:
                print(f"Failed to process {pid}, will retry")

        # If inbox is now empty, loop back to fetch more
        if not list(INBOX.glob("*.jsonl")):
            continue

        # Otherwise sleep briefly before checking again
        time.sleep(1)

    print("DGX2 Worker shutting down gracefully.")


if __name__ == "__main__":
    main()
