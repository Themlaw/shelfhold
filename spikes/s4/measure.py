"""Spike S4: container RSS at rest and after a simple load, compressed image size.

    SHELFHOLD_IMAGE=shelfhold:ci uv run python measure.py > results.json
The stack is e2e/compose.yaml (hardened, init: true, MALLOC_ARENA_MAX=2 from the image).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "e2e" / "compose.yaml")]
IMAGE = os.environ["SHELFHOLD_IMAGE"]

RSS_SCRIPT = r"""
import os, sys
for pid in filter(str.isdigit, os.listdir('/proc')):
    try:
        cmd = open(f'/proc/{pid}/cmdline').read().split('\0')
    except OSError:
        continue
    if any(c.endswith('shelfhold') for c in cmd) and 'serve' in cmd:
        for line in open(f'/proc/{pid}/status'):
            if line.startswith(('VmRSS', 'VmHWM', 'Threads')):
                print(line.split(':')[0], line.split()[1])
        for line in open(f'/proc/{pid}/smaps_rollup'):
            if line.startswith(('Pss:', 'Private_Dirty:', 'Anonymous:')):
                print(line.split(':')[0], line.split()[1])
"""

LOAD_SCRIPT = r"""
import threading, urllib.request
paths = ['/healthz', '/readyz', '/openapi.json', '/docs', '/nope']
errors = 0
def work():
    global errors
    for i in range(500):
        try:
            urllib.request.urlopen('http://127.0.0.1:8080' + paths[i % len(paths)], timeout=10).read()
        except Exception:
            errors += 1
threads = [threading.Thread(target=work) for _ in range(16)]
[t.start() for t in threads]; [t.join() for t in threads]
print('requests', 16 * 500, 'non-2xx or errors (404 on /nope expected)', errors)
"""


def run(*args: str, stdin: str | None = None) -> str:
    return subprocess.run([*COMPOSE, *args], input=stdin, capture_output=True, text=True, check=True).stdout


def rss() -> dict[str, float]:
    out = run("exec", "-T", "shelfhold", "python", "-c", RSS_SCRIPT)
    return {k: round(float(v) / 1024, 1) if k not in ("Threads",) else float(v)
            for k, v in (line.split() for line in out.splitlines())}


def cgroup_memory_mb() -> float:
    out = run("exec", "-T", "shelfhold", "python", "-c", "print(open('/sys/fs/cgroup/memory.current').read())")
    return round(int(out) / 1048576, 1)


run("down", "-v", "-t", "1")
run("up", "--wait", "-d")
try:
    time.sleep(30)
    idle = {"process_mb": rss(), "container_cgroup_mb": cgroup_memory_mb()}
    load_report = run("exec", "-T", "shelfhold", "python", "-c", LOAD_SCRIPT).strip()
    peak = {"process_mb": rss(), "container_cgroup_mb": cgroup_memory_mb()}
    time.sleep(15)
    after = {"process_mb": rss(), "container_cgroup_mb": cgroup_memory_mb()}
finally:
    run("down", "-v", "-t", "1")

size = subprocess.run([str(ROOT / "docker" / "check-image-size.sh"), IMAGE], capture_output=True, text=True, check=False)
uncompressed = subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Size}}"],
                              capture_output=True, text=True, check=True).stdout.strip()
print(json.dumps({
    "image": IMAGE, "arch": os.uname().machine,
    "image_size": {"compressed_gzip_of_docker_save": size.stdout.strip(), "uncompressed_mb": round(int(uncompressed) / 1e6, 1)},
    "rss_idle_30s_after_ready": idle, "load": load_report, "rss_right_after_load": peak, "rss_15s_after_load": after,
}, indent=2))
