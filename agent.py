#!/usr/bin/env python3
"""Nova Cloud node agent (Python 3, standard library only).

Every few seconds it tells the panel it is alive, reports its Docker VPS,
and runs the commands the panel queued (create, start, stop, restart,
reinstall, delete, sshx). It updates itself when the panel has a newer agent.
"""
AGENT_VERSION = 1

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

CONF = os.environ.get("NOVA_NODE_CONF", "/etc/nova-cloud/node.conf")
IMAGE = os.environ.get("NOVA_IMAGE", "ubuntu:22.04")
HB_SECS = 3


def read_conf():
    c = {}
    with open(CONF, "r", encoding="utf-8") as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                c[k] = v
    return c


conf = read_conf()
URL = conf["PANEL_URL"].rstrip("/")
TOKEN = conf["TOKEN"]


def api(path, data=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        URL + path, data=body, method="POST" if body is not None else "GET",
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def run(cmd, timeout=900):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:  # missing docker, timeout, ...
        return 1, str(e)


def info():
    mem = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    mem = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    return {
        "hostname": socket.gethostname(),
        "cpus": os.cpu_count() or 1,
        "mem_mb": mem,
        "disk_gb": shutil.disk_usage("/").total // 2**30,
        "load": round(os.getloadavg()[0], 2),
        "docker": bool(shutil.which("docker")),
        "kvm": os.path.exists("/dev/kvm"),
    }


def containers():
    rc, out = run(["docker", "ps", "-a", "--filter", "name=^nova-", "--format", "{{.Names}} {{.State}}"], 20)
    res = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                res[parts[0]] = parts[1]
    return res


# ---- commands -------------------------------------------------------------
def op_create(a):
    c = a["cname"]
    run(["docker", "rm", "-f", c], 60)
    rc, out = run(["docker", "run", "-d", "--name", c, "--hostname", a.get("name", c),
                   "--cpus", str(a["cpus"]), "--memory", str(a["mem"]) + "g",
                   "--restart", "unless-stopped", "--init", IMAGE, "sleep", "infinity"], 1500)
    if rc:
        return False, out[-300:]
    rc, out = run(["docker", "exec", c, "bash", "-c",
                   "apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates procps"], 1500)
    if rc:
        return False, "Container created but package install failed: " + out[-250:]
    return True, "created"


def simple(*args):
    def f(a):
        rc, out = run(list(args) + [a["cname"]], 120)
        return rc == 0, out[-300:] or "ok"
    return f


def op_sshx(a):
    c = a["cname"]
    run(["docker", "exec", c, "bash", "-c", "pkill -x sshx; rm -f /tmp/sshx.log; true"], 30)
    rc, out = run(["docker", "exec", "-d", c, "bash", "-c",
                   "curl -sSf https://sshx.io/get | sh -s run > /tmp/sshx.log 2>&1"], 30)
    if rc:
        return False, out[-300:]
    log = ""
    deadline = time.time() + 70
    while time.time() < deadline:
        time.sleep(2)
        _, log = run(["docker", "exec", c, "cat", "/tmp/sshx.log"], 15)
        m = re.search(r"https://sshx\.io/s/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+", log)
        if m:
            return True, m.group(0)
    return False, "sshx gave no link. Does the VPS have internet access? " + log[-200:]


def op_sshx_stop(a):
    run(["docker", "exec", a["cname"], "bash", "-c", "pkill -x sshx; true"], 30)
    return True, "stopped"


OPS = {
    "create": op_create,
    "reinstall": op_create,
    "start": simple("docker", "start"),
    "stop": simple("docker", "stop", "-t", "10"),
    "restart": simple("docker", "restart", "-t", "10"),
    "delete": simple("docker", "rm", "-f"),
    "sshx": op_sshx,
    "sshx_stop": op_sshx_stop,
}
locks = {}


def worker(cmd):
    lk = locks.setdefault(cmd["args"].get("cname", ""), threading.Lock())
    with lk:
        try:
            ok, out = OPS[cmd["op"]](cmd["args"])
        except Exception as e:
            ok, out = False, str(e)
    for _ in range(6):
        try:
            api("/api/agent/result", {"id": cmd["id"], "ok": bool(ok), "output": str(out)[-500:]})
            return
        except Exception:
            time.sleep(3)


def self_update():
    if threading.active_count() > 1:  # commands still running, try again later
        return
    try:
        with urllib.request.urlopen(URL + "/agent.py", timeout=20) as r:
            text = r.read().decode()
        compile(text, "agent.py", "exec")
        if "AGENT_VERSION" not in text:
            return
        path = os.path.realpath(sys.argv[0])
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        os._exit(0)  # systemd starts the new version
    except Exception:
        pass


def main():
    while True:
        try:
            payload = info()
            payload["containers"] = containers()
            resp = json.loads(api("/api/agent/heartbeat", payload))
            v = resp.get("agent_version")
            if v and v != AGENT_VERSION:
                self_update()
            for cmd in resp.get("cmds", []):
                threading.Thread(target=worker, args=(cmd,), daemon=True).start()
        except Exception:
            pass
        time.sleep(HB_SECS)


if __name__ == "__main__":
    main()
