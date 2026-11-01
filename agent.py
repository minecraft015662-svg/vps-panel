#!/usr/bin/env python3
"""Nova Cloud node agent (Python 3, standard library only).

Every few seconds it tells the panel it is alive, reports its Docker VPS,
and runs the commands the panel queued (create, start, stop, restart,
reinstall, delete, sshx). It updates itself when the panel has a newer agent.
"""
AGENT_VERSION = 3

import base64
import json
import os
import posixpath
import tempfile
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
HB_SECS = 1


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
def step(a, msg):
    try:
        api("/api/agent/progress", {"id": a.get("_id"), "msg": msg}, 10)
    except Exception:
        pass


NEOCONF = """[ -r /etc/nova-vps.env ] && . /etc/nova-vps.env
nova_uptime() {
    local s d h m
    s=$(ps -o etimes= -p 1 2>/dev/null | tr -d ' ')
    [ -n "$s" ] || { echo "unknown"; return; }
    d=$((s / 86400)); h=$((s % 86400 / 3600)); m=$((s % 3600 / 60))
    [ "$d" -gt 0 ] && printf '%s days, ' "$d"
    printf '%s hours, %s mins' "$h" "$m"
}
nova_mem() {
    local u
    if [ -r /sys/fs/cgroup/memory.current ]; then u=$(( $(cat /sys/fs/cgroup/memory.current) / 1048576 ))
    elif [ -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then u=$(( $(cat /sys/fs/cgroup/memory/memory.usage_in_bytes) / 1048576 ))
    else u=0; fi
    echo "${u}MiB / ${NOVA_MEM}MiB"
}
nova_disk() { echo "$(du -sxm / 2>/dev/null | cut -f1)MiB / ${NOVA_DISK}GB"; }
print_info() {
    info title
    info underline
    info "OS" distro
    prin "Host" "${NOVA_PANEL} Docker VPS"
    info "Kernel" kernel
    prin "Uptime" "$(nova_uptime)"
    info "Packages" packages
    info "Shell" shell
    prin "CPU" "${NOVA_CPUS} vCPU"
    prin "Memory" "$(nova_mem)"
    prin "Disk" "$(nova_disk)"
    info cols
}
"""


STATS = {}
DISK = {}


def _mb(s):
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", s or "")
    if not m:
        return 0.0
    f = {"b": 1 / 1048576, "kb": 1 / 1024, "kib": 1 / 1024, "mb": 1, "mib": 1, "gb": 1024, "gib": 1024, "tb": 1048576, "tib": 1048576}
    return float(m.group(1)) * f.get(m.group(2).lower(), 0)


def stats_loop():
    n = 0
    while True:
        try:
            rc, out = run(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"], 40)
            cur = {}
            if rc == 0:
                for line in out.splitlines():
                    p = line.split("|")
                    if len(p) == 3 and p[0].startswith("nova-"):
                        cur[p[0]] = {"cpu": round(float(p[1].strip().strip("%") or 0), 1), "mem": round(_mb(p[2].split("/")[0]))}
            if n % 12 == 0:
                rc2, out2 = run(["docker", "ps", "-s", "--filter", "name=^nova-", "--format", "{{.Names}}|{{.Size}}"], 90)
                if rc2 == 0:
                    for line in out2.splitlines():
                        p = line.split("|", 1)
                        if len(p) == 2 and p[1].split():
                            m = re.search(r"virtual ([\d.]+\s*[A-Za-z]+)", p[1])
                            DISK[p[0]] = round(_mb(m.group(1) if m else p[1].split()[0]))
            for k, v in cur.items():
                v["disk"] = DISK.get(k, 0)
            STATS.clear()
            STATS.update(cur)
        except Exception:
            pass
        n += 1
        time.sleep(3)


def put_file(c, path, text):
    run(["docker", "exec", c, "mkdir", "-p", posixpath.dirname(path)], 20)
    with tempfile.NamedTemporaryFile("w", delete=False) as t:
        t.write(text)
        tmp = t.name
    try:
        return run(["docker", "cp", tmp, c + ":" + path], 30)
    finally:
        os.unlink(tmp)


def op_create(a):
    c = a["cname"]
    panel = re.sub(r"[^A-Za-z0-9 ._-]", "", str(a.get("panel") or "Nova Cloud")).strip()[:30] or "Nova Cloud"
    host = re.sub(r"[^a-z0-9-]", "", str(a.get("host") or "nova").lower())[:30] or "nova"
    step(a, "Removing old container")
    run(["docker", "rm", "-f", c], 60)
    step(a, "Downloading the OS image")
    rc, out = run(["docker", "pull", IMAGE], 1500)
    if rc:
        return False, "Image download failed: " + out[-250:]
    step(a, "Creating the container")
    rc, out = run(["docker", "run", "-d", "--name", c, "--hostname", host,
                   "--cpus", str(a["cpus"]), "--memory", str(a["mem"]) + "g",
                   "--restart", "unless-stopped", "--init", IMAGE, "sleep", "infinity"], 1500)
    if rc:
        return False, out[-300:]
    step(a, "Installing packages (1-3 minutes)")
    rc, out = run(["docker", "exec", c, "bash", "-c",
                   "apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y curl wget nano sudo ca-certificates procps"], 1500)
    if rc:
        return False, "Container created but package install failed: " + out[-250:]
    step(a, "Applying panel settings")
    run(["docker", "exec", c, "bash", "-c", "DEBIAN_FRONTEND=noninteractive apt-get install -y neofetch"], 600)
    env = "NOVA_PANEL='%s'\nNOVA_NAME='%s'\nNOVA_CPUS=%d\nNOVA_MEM=%d\nNOVA_DISK=%d\n" % (
        panel, host, int(float(a["cpus"])), int(a["mem"]) * 1024, int(a.get("disk") or 40))
    put_file(c, "/etc/nova-vps.env", env)
    put_file(c, "/root/.config/neofetch/config.conf", NEOCONF)
    return True, "created"


def fs_ls(a):
    return _ok(run(["docker", "exec", a["cname"], "find", a["path"], "-maxdepth", "1", "-mindepth", "1",
                    "-printf", "%y|%s|%T@|%f\n"], 30))


def _ok(r):
    return r[0] == 0, r[1]


def fs_get(a):
    sh = ('[ -f "$1" ] || { echo "Not a file"; exit 1; }; '
          '[ "$(stat -c %s "$1")" -le 5242880 ] || { echo "File is larger than 5 MB"; exit 1; }; base64 -w0 "$1"')
    return _ok(run(["docker", "exec", a["cname"], "sh", "-c", sh, "sh", a["path"]], 60))


def fs_write(a):
    raw = base64.b64decode(a["data"])
    with tempfile.NamedTemporaryFile(delete=False) as t:
        t.write(raw)
        tmp = t.name
    try:
        rc, out = run(["docker", "cp", tmp, a["cname"] + ":" + a["path"]], 60)
    finally:
        os.unlink(tmp)
    if rc == 0:
        run(["docker", "exec", a["cname"], "chmod", "644", a["path"]], 20)
    return rc == 0, out or "saved"


def fs_rm(a):
    p = posixpath.normpath(a["path"])
    if len([x for x in p.split("/") if x]) < 2:
        return False, "Refusing to delete a top-level system folder"
    return _ok(run(["docker", "exec", a["cname"], "rm", "-rf", "--", p], 60))


def fs_mkdir(a):
    return _ok(run(["docker", "exec", a["cname"], "mkdir", "-p", "--", a["path"]], 30))


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
    "fs_ls": fs_ls, "fs_get": fs_get, "fs_write": fs_write, "fs_rm": fs_rm, "fs_mkdir": fs_mkdir,
}
locks = {}


def worker(cmd):
    cmd["args"]["_id"] = cmd["id"]
    lk = threading.Lock() if cmd["op"].startswith("fs_") else locks.setdefault(cmd["args"].get("cname", ""), threading.Lock())
    with lk:
        try:
            ok, out = OPS[cmd["op"]](cmd["args"])
        except Exception as e:
            ok, out = False, str(e)
    for _ in range(6):
        try:
            api("/api/agent/result", {"id": cmd["id"], "ok": bool(ok), "output": str(out) if cmd["op"].startswith("fs_") else str(out)[-500:]}, 90)
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
    threading.Thread(target=stats_loop, daemon=True).start()
    while True:
        try:
            payload = info()
            payload["containers"] = containers()
            payload["stats"] = dict(STATS)
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
