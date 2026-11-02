#!/usr/bin/env python3
"""Nova Cloud node agent (Python 3, standard library only).

Every few seconds it tells the panel it is alive, reports its Docker VPS,
and runs the commands the panel queued (create, start, stop, restart,
reinstall, delete, sshx). It updates itself when the panel has a newer agent.

VPS are Docker containers that run systemd as PID 1 (privileged, own /var/lib/docker),
so a full Pterodactyl Panel and Wings can be installed inside them.
"""
AGENT_VERSION = 7

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
# OS choices the panel offers
OS_IMAGES = {"ubuntu20": "ubuntu:20.04", "ubuntu22": "ubuntu:22.04", "ubuntu24": "ubuntu:24.04",
             "debian11": "debian:11", "debian12": "debian:12", "debian13": "debian:13",
             "rocky9": "rockylinux:9", "alma9": "almalinux:9"}
RPM_OS = ("rocky9", "alma9")   # dnf based, everything else is apt based
BASE_REV = "v6"                # bump to rebuild the cached systemd base images
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


def run(cmd, timeout=900, inp=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=inp)
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
nova_cpu() {
    if [ -n "$NOVA_CPU_NAME" ]; then echo "${NOVA_CPU_NAME} (${NOVA_CPUS})"; else echo "${NOVA_CPUS} vCPU"; fi
}
print_info() {
    info title
    info underline
    info "OS" distro
    prin "Host" "${NOVA_PANEL} Docker VPS"
    info "Kernel" kernel
    prin "Uptime" "$(nova_uptime)"
    info "Packages" packages
    info "Shell" shell
    prin "CPU" "$(nova_cpu)"
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
        time.sleep(1.5)


def clean_cpu(raw):
    return re.sub(r"[^A-Za-z0-9 ._@()/+,-]", "", str(raw or "")).strip()[:60]


def put_file(c, path, text):
    run(["docker", "exec", c, "mkdir", "-p", posixpath.dirname(path)], 20)   # fails on a stopped VPS, docker cp below still works
    with tempfile.NamedTemporaryFile("w", delete=False) as t:
        t.write(text)
        tmp = t.name
    os.chmod(tmp, 0o644)
    try:
        return run(["docker", "cp", tmp, c + ":" + path], 30)
    finally:
        os.unlink(tmp)


def cgroup_v2():
    return os.path.exists("/sys/fs/cgroup/cgroup.controllers")


def docker_run(a, host, image, name):
    """Start a VPS container. The image's own CMD (/sbin/init = systemd) is PID 1.
    --privileged + its own /var/lib/docker volume let Docker (and so Wings) run inside the VPS."""
    cmd = ["docker", "run", "-d", "--name", name, "--hostname", host,
           "--cpus", str(a["cpus"]), "--memory", str(a["mem"]) + "g",
           "--restart", "unless-stopped", "--privileged",
           "--tmpfs", "/run", "--tmpfs", "/run/lock",
           "-v", name + "-docker:/var/lib/docker"]
    if cgroup_v2():
        cmd += ["--cgroupns=private"]
    else:
        cmd += ["--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw"]
    return run(cmd + [image], 1500)


MASK = ("systemd-udevd.service systemd-udevd-kernel.socket systemd-udevd-control.socket "
        "systemd-udev-trigger.service systemd-modules-load.service sys-kernel-debug.mount "
        "sys-kernel-tracing.mount systemd-remount-fs.service getty.target console-getty.service")


def dockerfile(os_key, image):
    if os_key in RPM_OS:
        inst = ("dnf -y install systemd curl wget nano sudo ca-certificates procps-ng iproute iptables "
                "passwd findutils && dnf clean all")
        init = "/usr/sbin/init"
    else:
        inst = ("apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv dbus "
                "curl wget nano sudo ca-certificates procps iproute2 iptables findutils && "
                "rm -rf /var/lib/apt/lists/*")
        init = "/sbin/init"
    return ("FROM %s\nENV container=docker DEBIAN_FRONTEND=noninteractive\nRUN %s\n"
            "RUN mkdir -p /etc/docker && echo '{\"bip\":\"10.88.0.1/24\"}' > /etc/docker/daemon.json\n"
            "RUN systemctl mask %s || true\nSTOPSIGNAL SIGRTMIN+3\nCMD [\"%s\"]\n" % (image, inst, MASK, init))


def base_image(os_key, image, a):
    """Build (once per OS) an image that has systemd + the basic tools. Returns (tag, error)."""
    tag = "nova-base-%s:%s" % (os_key, BASE_REV)
    if run(["docker", "image", "inspect", tag], 30)[0] == 0:
        return tag, ""
    step(a, "Building the %s systemd image (first time only, 1-3 minutes)" % os_key)
    rc, out = run(["docker", "build", "--pull", "-t", tag, "-"], 1800, inp=dockerfile(os_key, image))
    if rc:
        return None, "Image build failed: " + out[-250:]
    return tag, ""


def wait_systemd(c, secs=60):
    end = time.time() + secs
    st = ""
    while time.time() < end:
        _, st = run(["docker", "exec", c, "systemctl", "is-system-running"], 10)
        if st.strip() in ("running", "degraded"):
            return True
        time.sleep(2)
    return False


def op_create(a):
    c = a["cname"]
    os_key = str(a.get("os"))
    panel = re.sub(r"[^A-Za-z0-9 ._-]", "", str(a.get("panel") or "Nova Cloud")).strip()[:30] or "Nova Cloud"
    host = re.sub(r"[^a-z0-9-]", "", str(a.get("host") or "nova").lower())[:30] or "nova"
    step(a, "Removing old container")
    run(["docker", "rm", "-f", c], 60)
    run(["docker", "volume", "rm", "-f", c + "-docker"], 60)
    image = OS_IMAGES.get(os_key, IMAGE)
    if os_key not in OS_IMAGES:
        os_key = "ubuntu22"
    tag, err = base_image(os_key, image, a)
    if not tag:
        return False, err
    step(a, "Creating the container (systemd)")
    rc, out = docker_run(a, host, tag, c)
    if rc:
        return False, out[-300:]
    step(a, "Waiting for systemd to boot")
    if not wait_systemd(c):
        return False, "The container started but systemd did not boot. On the node check: docker logs " + c
    if os_key not in RPM_OS:
        step(a, "Updating package lists")
        run(["docker", "exec", c, "bash", "-c", "DEBIAN_FRONTEND=noninteractive apt-get update -y"], 600)
        step(a, "Applying panel settings")
        run(["docker", "exec", c, "bash", "-c", "DEBIAN_FRONTEND=noninteractive apt-get install -y neofetch"], 600)
    env = "NOVA_PANEL='%s'\nNOVA_NAME='%s'\nNOVA_CPUS=%d\nNOVA_MEM=%d\nNOVA_DISK=%d\nNOVA_CPU_NAME='%s'\n" % (
        panel, host, int(float(a["cpus"])), int(a["mem"]) * 1024, int(a.get("disk") or 40), clean_cpu(a.get("cpu_name")))
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


def read_file(c, path):
    """Read a file of a VPS with docker cp (works for running and stopped containers)."""
    with tempfile.TemporaryDirectory() as d:
        rc, _ = run(["docker", "cp", c + ":" + path, d + "/f"], 30)
        if rc:
            return ""
        with open(d + "/f", "r", encoding="utf-8", errors="replace") as f:
            return f.read()


def set_env(c, kv):
    """Change / add NAME=value lines in /etc/nova-vps.env of a VPS."""
    lines = [l for l in read_file(c, "/etc/nova-vps.env").splitlines() if l.split("=", 1)[0] not in kv]
    lines += ["%s=%s" % (k, v) for k, v in kv.items()]
    return put_file(c, "/etc/nova-vps.env", "\n".join(lines) + "\n")


def op_resize(a):
    """Change CPU / RAM / CPU name of an existing container without touching its data."""
    c = a["cname"]
    cpus, mem, disk = int(float(a["cpus"])), int(a["mem"]), int(a["disk"])
    rc, out = run(["docker", "update", "--cpus", str(cpus), "--memory", "%dg" % mem,
                   "--memory-swap", "%dg" % (mem * 2), c], 120)
    if rc:
        return False, out[-300:]
    # keep neofetch inside the VPS in sync (also works when the VPS is stopped)
    kv = {"NOVA_CPUS": str(cpus), "NOVA_MEM": str(mem * 1024), "NOVA_DISK": str(disk)}
    if a.get("cpu_name") is not None:
        kv["NOVA_CPU_NAME"] = "'%s'" % clean_cpu(a.get("cpu_name"))
    set_env(c, kv)
    put_file(c, "/root/.config/neofetch/config.conf", NEOCONF)   # old VPS get the new CPU line too
    return True, "resized"


def op_backup(a):
    """A backup is a docker image committed from the container (the container keeps running)."""
    rc, out = run(["docker", "commit", a["cname"], a["img"]], 1800)
    if rc:
        return False, out[-300:]
    rc, out = run(["docker", "image", "inspect", "-f", "{{.Size}}", a["img"]], 60)
    return True, out.strip() if rc == 0 else "0"


def op_restore(a):
    """Start a new container from a backup image. The old one is kept until the new one runs."""
    c, old = a["cname"], a["cname"] + "-old"
    host = re.sub(r"[^a-z0-9-]", "", str(a.get("host") or "nova").lower())[:30] or "nova"
    run(["docker", "rm", "-f", old], 60)
    run(["docker", "stop", "-t", "10", c], 60)
    rc, out = run(["docker", "rename", c, old], 30)
    if rc:
        return False, "Could not prepare the restore: " + out[-200:]
    step(a, "Restoring the backup")
    rc, out = docker_run(a, host, a["img"], c)
    if rc:
        run(["docker", "rm", "-f", c], 30)
        run(["docker", "rename", old, c], 30)
        run(["docker", "start", c], 60)
        return False, "Restore failed, the VPS was left unchanged: " + out[-200:]
    run(["docker", "rm", "-f", old], 60)
    return True, "restored"


def op_delete(a):
    rc, out = run(["docker", "rm", "-f", a["cname"]], 120)
    run(["docker", "volume", "rm", "-f", a["cname"] + "-docker"], 60)
    for img in a.get("imgs") or []:
        run(["docker", "rmi", "-f", img], 120)
    return rc == 0, out[-300:] or "ok"


def op_bkdel(a):
    return _ok(run(["docker", "rmi", "-f", a["img"]], 120))


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


# ---- port forwarding -----------------------------------------------------------------------------------
# The panel says which ports each VPS may use. Here they become real: traffic that arrives on the node
# at port P is sent to the same port P of the VPS (iptables DNAT, TCP and UDP). A port that a program on
# the node itself is listening on (ssh, nginx, the panel) is never taken over.
FWD = {"want": {}, "status": {}}
FWD_LOCK = threading.Lock()


def host_listen_ports():
    rc, out = run(["ss", "-H", "-ltnu"], 10)
    ports = set()
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            m = re.search(r":(\d+)$", parts[4]) if len(parts) >= 5 else None
            if m:
                ports.add(int(m.group(1)))
    return ports


def container_ips():
    rc, out = run(["docker", "ps", "-q", "--filter", "name=^nova-"], 20)
    ids = out.split() if rc == 0 else []
    ips = {}
    if ids:
        rc, out = run(["docker", "inspect", "-f", "{{.Name}}|{{.NetworkSettings.IPAddress}}"] + ids, 30)
        if rc == 0:
            for line in out.splitlines():
                name, _, ip = line.partition("|")
                ips[name.lstrip("/")] = ip.strip()
    return ips


def fwd_plan(want, ips, busy):
    """-> (list of (ip, port) to forward, {cname: {port: status}})"""
    rules, status = [], {}
    for cname, ports in want.items():
        ip = ips.get(cname, "")
        for p in ports:
            if p in busy:
                st = "busy"
            elif not ip:
                st = "noip"
            else:
                st = "open"
                rules.append((ip, p))
            status.setdefault(cname, {})[str(p)] = st
    return rules, status


def ipt(*args):
    return run(["iptables"] + list(args), 15)


def fwd_ensure():
    """Create our chains and hook them in. Returns True when something had to be (re)created."""
    fresh = False
    if ipt("-t", "nat", "-N", "NOVA-DNAT")[0] == 0:
        fresh = True
    hook = ["-m", "addrtype", "--dst-type", "LOCAL", "-j", "NOVA-DNAT"]
    if ipt("-t", "nat", "-C", "PREROUTING", *hook)[0] != 0:
        ipt("-t", "nat", "-I", "PREROUTING", "1", *hook)
        fresh = True
    if ipt("-N", "NOVA-FWD")[0] == 0:
        fresh = True
    parent = "DOCKER-USER" if ipt("-S", "DOCKER-USER")[0] == 0 else "FORWARD"
    if ipt("-C", parent, "-j", "NOVA-FWD")[0] != 0:
        ipt("-I", parent, "1", "-j", "NOVA-FWD")
        fresh = True
    return fresh


def fwd_apply(rules):
    bad = set()
    ipt("-t", "nat", "-F", "NOVA-DNAT")
    ipt("-F", "NOVA-FWD")
    for ip, p in rules:
        for proto in ("tcp", "udp"):
            r1 = ipt("-t", "nat", "-A", "NOVA-DNAT", "-p", proto, "--dport", str(p), "-j", "DNAT",
                     "--to-destination", "%s:%d" % (ip, p))
            r2 = ipt("-A", "NOVA-FWD", "-p", proto, "-d", ip, "--dport", str(p), "-j", "ACCEPT")
            if r1[0] or r2[0]:
                bad.add((ip, p))
    return bad


def fwd_loop():
    last = None
    while True:
        try:
            with FWD_LOCK:
                want = {k: list(v) for k, v in FWD["want"].items()}
            rules, status = fwd_plan(want, container_ips() if want else {}, host_listen_ports() if want else set())
            fresh = fwd_ensure()
            sig = sorted(rules)
            if sig != last or fresh:
                bad = fwd_apply(rules)
                last = sig
                if bad:
                    ipmap = {(ip, p) for ip, p in bad}
                    ips = container_ips()
                    for cname, ports in status.items():
                        for p, st in list(ports.items()):
                            if st == "open" and (ips.get(cname), int(p)) in ipmap:
                                ports[p] = "error"
            with FWD_LOCK:
                FWD["status"] = status
        except Exception:
            pass
        time.sleep(3)


OPS = {
    "create": op_create,
    "reinstall": op_create,
    "start": simple("docker", "start"),
    "stop": simple("docker", "stop", "-t", "10"),
    "restart": simple("docker", "restart", "-t", "10"),
    "delete": op_delete,
    "resize": op_resize,
    "backup": op_backup,
    "restore": op_restore,
    "bkdel": op_bkdel,
    "sshx": op_sshx,
    "sshx_stop": op_sshx_stop,
    "fs_ls": fs_ls, "fs_get": fs_get, "fs_write": fs_write, "fs_rm": fs_rm, "fs_mkdir": fs_mkdir,
}
locks = {}
RUNNING = [0]   # commands that are running right now


def worker(cmd):
    cmd["args"]["_id"] = cmd["id"]
    lk = threading.Lock() if cmd["op"].startswith("fs_") else locks.setdefault(cmd["args"].get("cname", ""), threading.Lock())
    RUNNING[0] += 1
    try:
        with lk:
            try:
                ok, out = OPS[cmd["op"]](cmd["args"])
            except Exception as e:
                ok, out = False, str(e)
    finally:
        RUNNING[0] -= 1
    for _ in range(6):
        try:
            api("/api/agent/result", {"id": cmd["id"], "ok": bool(ok), "output": str(out) if cmd["op"].startswith("fs_") else str(out)[-500:]}, 90)
            return
        except Exception:
            time.sleep(3)


def self_update():
    if RUNNING[0] > 0:  # commands still running, try again later
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
    threading.Thread(target=fwd_loop, daemon=True).start()
    while True:
        try:
            payload = info()
            payload["containers"] = containers()
            payload["stats"] = dict(STATS)
            with FWD_LOCK:
                payload["fwd"] = dict(FWD["status"])
            resp = json.loads(api("/api/agent/heartbeat", payload))
            if isinstance(resp.get("ports"), dict):
                with FWD_LOCK:
                    FWD["want"] = {k: [int(x) for x in v] for k, v in resp["ports"].items()}
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
