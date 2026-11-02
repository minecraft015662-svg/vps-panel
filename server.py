#!/usr/bin/env python3
"""Nova Cloud panel backend. Python 3 standard library only.

Browser:  /api/login /api/logout /api/state /api/vps /api/vps/<id>/<op>
Agents:   /api/agent/heartbeat /api/agent/result   (Bearer node token)
Public:   / (index.html)  /agent.py (node agent, no secrets)
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
PANEL_DIR = os.environ.get("NOVA_PANEL_DIR", BASE)
DATA = os.environ.get("NOVA_DATA", os.path.join(BASE, "data.json"))
PORT = int(os.environ.get("NOVA_PORT", "3000"))
HOST = os.environ.get("NOVA_HOST", "127.0.0.1")
ADMIN_USER = os.environ.get("NOVA_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("NOVA_ADMIN_PASS", "admin")
ONLINE_SECS = 30
SESSION_SECS = 7 * 24 * 3600
CMD_TIMEOUT = 1800

BUSY = {"installing", "starting", "stopping", "restarting", "reinstalling", "restoring"}
# fields the browser may never overwrite (the server is the only writer)
SERVER_OWNED = ("st", "ssh", "err", "cname", "sshwait", "step", "cpu_pct", "mem_mb", "disk_mb",
                "cpus", "mem", "disk", "plan", "os", "bk_limit", "ports", "bk", "sysd", "pst")

OS_LABELS = {"ubuntu20": "Ubuntu 20.04 LTS", "ubuntu22": "Ubuntu 22.04 LTS", "ubuntu24": "Ubuntu 24.04 LTS",
             "debian11": "Debian 11", "debian12": "Debian 12"}
DEF_OS, DEF_BK = "ubuntu22", 2   # defaults: OS, backups allowed per VPS

lock = threading.Lock()
sessions = {}  # sid -> (user id, expiry)
results = {}   # file-manager command id -> (time, ok, output)


def plan_str(v):
    return "%d vCPU, %d GB RAM, %d GB disk" % (v["cpus"], v["mem"], v["disk"])


def norm_vps(v):
    """Make sure every VPS has the OS / limit / port / backup fields (old data has none)."""
    if v.get("os") not in OS_LABELS:
        v["os"] = DEF_OS
    v.setdefault("bk_limit", DEF_BK)
    v.pop("port_limit", None)   # ports have no limit any more: the admin adds exactly the ports a VPS may use
    if not isinstance(v.get("ports"), list):
        v["ports"] = []
    if not isinstance(v.get("bk"), list):
        v["bk"] = []
    v["bk"] = [b for b in v["bk"] if isinstance(b, dict) and isinstance(b.get("id"), int)]
    v["sysd"] = bool(v.get("sysd"))          # systemd + nested Docker mode (Pterodactyl panel / Wings)
    if not isinstance(v.get("pst"), dict):   # port forwarding status reported by the node
        v["pst"] = {}
    return v


def truthy(x):
    return x is True or str(x).strip().lower() in ("1", "true", "yes", "on")


def num(d, key, lo, hi, dflt):
    raw = d.get(key)
    if raw is None or raw == "":
        return dflt
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % key)
    return max(lo, min(hi, n))


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()


def check_pw(pw, h):
    try:
        return hmac.compare_digest(hash_pw(pw, h.split("$", 1)[0]), h)
    except Exception:
        return False


def default_state():
    return {
        "users": [{"id": 1, "name": "Admin", "email": "admin", "role": "admin"}],
        "nodes": [],
        "vps": [],
        "nid": 10,
        "cid": 1,
        "cmds": [],
        "notifs": [],
        "rev": 0,
    }


def load_state():
    for p in (DATA, DATA + ".bak"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k, v in default_state().items():
                d.setdefault(k, v)
            for v in d.get("vps", []):
                norm_vps(v)
            return d
        except Exception:
            continue
    return default_state()


def save_state():
    os.makedirs(os.path.dirname(os.path.abspath(DATA)), exist_ok=True)
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(state, cmds=[c for c in state["cmds"] if not str(c["op"]).startswith("fs_")]), f)
    if os.path.exists(DATA):
        try:
            shutil.copy2(DATA, DATA + ".bak")
        except OSError:
            pass
    os.replace(tmp, DATA)


state = load_state()
SESS = DATA + ".sessions"


def load_sessions():
    try:
        with open(SESS, "r", encoding="utf-8") as f:
            return {k: (v[0], v[1]) for k, v in json.load(f).items() if v[1] > time.time()}
    except Exception:
        return {}


def save_sessions():
    try:
        tmp = SESS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sessions, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESS)
    except OSError:
        pass


sessions.update(load_sessions())


def agent_version():
    try:
        with open(os.path.join(PANEL_DIR, "agent.py"), "r", encoding="utf-8") as f:
            m = re.search(r"^AGENT_VERSION\s*=\s*(\d+)", f.read(), re.M)
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def notify(uid, title, msg):
    state["notifs"].append({"id": state["nid"], "user": uid, "title": title, "msg": msg, "t": time.time(), "read": False})
    state["nid"] += 1
    state["notifs"] = state["notifs"][-500:]


def online(n):
    return bool(n.get("seen")) and time.time() - n["seen"] <= ONLINE_SECS


def find(lst, i):
    for x in lst:
        if str(x.get("id")) == str(i):
            return x
    return None


def view(user):
    out = json.loads(json.dumps(state))
    out.pop("cmds", None)
    out.pop("cid", None)
    out["notifs"] = [dict(n) for n in reversed(state["notifs"]) if n["user"] == user["id"]][:50]
    for u in out["users"]:
        u.pop("pw", None)
    for n in out["nodes"]:
        n["status"] = "connecting" if not n.get("seen") else ("online" if online(n) else "offline")
    if user["role"] != "admin":
        out["users"] = [u for u in out["users"] if u["id"] == user["id"]]
        out["vps"] = [v for v in out["vps"] if v["user"] == user["id"]]
        out["nodes"] = [{"id": n["id"], "name": n["name"], "status": n["status"], "ip": n.get("ip", "")}
                        for n in out["nodes"] if any(v["node"] == n["id"] for v in out["vps"])]
    out["me"] = {"id": user["id"], "name": user["name"], "role": user["role"], "last": user.get("prev")}
    return out


def enqueue(node_id, op, vps, args):
    c = {"id": state["cid"], "node": node_id, "vps": vps["id"] if vps else 0,
         "op": op, "args": args, "status": "pending", "t": time.time()}
    state["cid"] += 1
    state["cmds"].append(c)
    state["cmds"] = state["cmds"][-300:]
    return c


def run_args(v):
    return {"cname": v["cname"], "name": v["name"], "cpus": v["cpus"], "mem": v["mem"], "disk": v["disk"],
            "panel": v.get("panel", "Nova Cloud"), "host": v.get("host", "nova"),
            "os": v.get("os", DEF_OS), "sysd": bool(v.get("sysd"))}


def clean_info(d):
    info = {}
    for k in ("hostname", "cpus", "mem_mb", "disk_gb", "load", "docker", "kvm"):
        v = d.get(k)
        if isinstance(v, (str, int, float, bool)):
            info[k] = v[:80] if isinstance(v, str) else v
    return info


def create_vps(d):
    name = str(d.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise ValueError("VPS name: 1-32 letters, numbers, - or _")
    if str(d.get("type", "docker")) != "docker":
        raise ValueError("Only Docker VPS are supported right now")
    node = find(state["nodes"], d.get("node"))
    if not node:
        raise ValueError("Select a node")
    if not online(node):
        raise ValueError("This node is not online")
    owner = find(state["users"], d.get("user"))
    cpus = max(1, min(64, int(d.get("cpus") or 2)))
    mem = max(1, min(1024, int(d.get("mem") or 4)))
    disk = max(1, min(100000, int(d.get("disk") or 40)))
    panel = re.sub(r"[^A-Za-z0-9 ._-]", "", str(d.get("panel") or "Nova Cloud")).strip()[:30] or "Nova Cloud"
    host = re.sub(r"[^a-z0-9]", "", panel.lower())[:20] or "nova"
    os_key = str(d.get("os") or DEF_OS)
    if os_key not in OS_LABELS:
        raise ValueError("Unknown operating system")
    bk_limit = num(d, "bk_limit", 0, 50, DEF_BK)
    sysd = truthy(d.get("sysd"))
    vid = state["nid"]
    state["nid"] += 1
    v = {"id": vid, "name": name, "panel": panel, "host": host, "user": owner["id"] if owner else 1, "node": node["id"],
         "type": "docker", "cpus": cpus, "mem": mem, "disk": disk,
         "plan": "%d vCPU, %d GB RAM, %d GB disk" % (cpus, mem, disk),
         "st": "installing", "cname": "nova-%d" % vid, "err": "", "ssh": "",
         "os": os_key, "bk_limit": bk_limit, "ports": [], "bk": [],
         "sysd": sysd, "pst": {}}
    state["vps"].append(v)
    enqueue(node["id"], "create", v, run_args(v))
    save_state()
    return v


def vps_op(vid, op, user):
    v = find(state["vps"], vid)
    if not v or (user["role"] != "admin" and (v["user"] != user["id"] or op == "delete")):
        raise LookupError("VPS not found")
    node = find(state["nodes"], v["node"])
    if op == "delete":
        state["vps"].remove(v)
        if node:
            enqueue(node["id"], "delete", v, {"cname": v["cname"], "imgs": [b.get("img") for b in v.get("bk", []) if b.get("img")]})
        save_state()
        return v
    if not node or not online(node):
        raise ValueError("The node of this VPS is not online")
    if v["st"] in BUSY:
        raise ValueError("VPS is busy (%s). Wait for it to finish" % v["st"])
    st = v["st"]
    if op == "start":
        if st == "running":
            raise ValueError("VPS is already running")
        v["st"] = "starting"
        enqueue(node["id"], "start", v, {"cname": v["cname"]})
    elif op == "stop":
        if st != "running":
            raise ValueError("VPS is not running")
        v["st"] = "stopping"
        enqueue(node["id"], "stop", v, {"cname": v["cname"]})
    elif op == "restart":
        v["st"] = "restarting"
        enqueue(node["id"], "restart", v, {"cname": v["cname"]})
    elif op == "reinstall":
        v["st"] = "reinstalling"
        enqueue(node["id"], "reinstall", v, run_args(v))
    elif op == "sshx":
        if st != "running":
            raise ValueError("Start the VPS first")
        if v.get("sshwait"):
            raise ValueError("A link is already being generated")
        v["sshwait"] = True
        v["ssh"] = ""
        v["err"] = ""
        enqueue(node["id"], "sshx", v, {"cname": v["cname"]})
    elif op == "sshx_stop":
        v["ssh"] = ""
        enqueue(node["id"], "sshx_stop", v, {"cname": v["cname"]})
    else:
        raise LookupError("Unknown action")
    if op in ("start", "stop", "restart", "reinstall"):
        v["err"] = ""
        v["ssh"] = ""
        v["sshwait"] = False
    save_state()
    return v


def apply_result(cmd, ok, out):
    cmd["status"] = "done" if ok else "error"
    cmd["out"] = out[:500]
    if str(cmd["op"]).startswith("fs_"):
        results[cmd["id"]] = (time.time(), ok, out)
        cmd["out"] = ""
        return
    v = find(state["vps"], cmd["vps"])
    if not v:
        return
    v["step"] = ""
    if cmd["op"] in ("create", "reinstall", "restore"):
        notify(v["user"], "VPS " + ("ready" if ok else "failed"), "%s: %s" % (v["name"], "your VPS is running." if ok else out[:120]))
    op = cmd["op"]
    if op in ("create", "start", "restart", "reinstall", "restore"):
        v["st"] = "running" if ok else "error"
        v["err"] = "" if ok else out[:300]
    elif op == "stop":
        v["st"] = "stopped" if ok else "running"
        v["err"] = "" if ok else out[:300]
    elif op == "sshx":
        v["sshwait"] = False
        v["ssh"] = out if ok else ""
        v["err"] = "" if ok else out[:300]
    elif op == "sshx_stop":
        v["ssh"] = ""
    elif op == "backup":
        b = find(v.get("bk", []), cmd["args"].get("bid"))
        if b:
            if ok:
                m = re.search(r"\d+", out)
                b["st"] = "ready"
                b["size"] = int(m.group(0)) if m else 0
                notify(v["user"], "Backup ready", "%s: %s" % (v["name"], b["n"]))
            else:
                v["bk"].remove(b)
                v["err"] = out[:300]
                notify(v["user"], "Backup failed", "%s: %s" % (v["name"], out[:120]))
    elif op == "resize" and not ok:
        for k, val in (cmd["args"].get("old") or {}).items():
            if k in ("cpus", "mem", "disk"):
                v[k] = val
        v["plan"] = plan_str(v)
        v["err"] = out[:300]
        notify(v["user"], "Resize failed", "%s: %s" % (v["name"], out[:120]))


def sync_fwd(node, fwd):
    """The node reports per container and port: open / busy / noip / error."""
    if not isinstance(fwd, dict):
        return
    for v in state["vps"]:
        if v["node"] != node["id"]:
            continue
        r = fwd.get(v.get("cname"))
        v["pst"] = {str(k): x for k, x in r.items() if x in ("open", "busy", "noip", "error")} if isinstance(r, dict) else {}


def wanted_ports(node):
    return {v["cname"]: [p["p"] for p in v.get("ports", []) if isinstance(p.get("p"), int)]
            for v in state["vps"] if v["node"] == node["id"] and v.get("ports")}


def sweep_and_sync(node, containers, stats=None):
    now = time.time()
    for c in state["cmds"]:
        if c["status"] == "sent" and now - c.get("sent", now) > CMD_TIMEOUT:
            apply_result(c, False, "Command timed out")
    if isinstance(containers, dict):
        for v in state["vps"]:
            if v["node"] != node["id"] or v["st"] in BUSY:
                continue
            s = containers.get(v.get("cname"))
            if s is not None and v["st"] in ("running", "stopped", "error"):
                v["st"] = "running" if s == "running" else "stopped"
                if s != "running":
                    v["ssh"] = ""
    if isinstance(stats, dict):
        for v in state["vps"]:
            if v["node"] != node["id"]:
                continue
            st = stats.get(v.get("cname"))
            if v["st"] == "running" and isinstance(st, dict):
                try:
                    v["cpu_pct"] = float(st.get("cpu", 0))
                    v["mem_mb"] = int(st.get("mem", 0))
                    v["disk_mb"] = int(st.get("disk", 0))
                except (TypeError, ValueError):
                    pass
            else:
                v.pop("cpu_pct", None)
                v.pop("mem_mb", None)


FS_ACTS = ("ls", "get", "write", "rm", "mkdir")


def fs_start(user, vid, d):
    for k in [k for k, r in results.items() if time.time() - r[0] > 120]:
        del results[k]
    v = find(state["vps"], vid)
    if not v or (user["role"] != "admin" and v["user"] != user["id"]):
        raise LookupError("VPS not found")
    node = find(state["nodes"], v["node"])
    if not node or not online(node):
        raise ValueError("The node of this VPS is not online")
    if v["st"] != "running":
        raise ValueError("Start the VPS first")
    act = str(d.get("act"))
    path = str(d.get("path", "/"))
    if act not in FS_ACTS:
        raise LookupError("Unknown action")
    if not path.startswith("/") or "\x00" in path or len(path) > 500:
        raise ValueError("Bad path")
    args = {"cname": v["cname"], "path": path}
    if act == "write":
        data = str(d.get("data", ""))
        if len(data) > 7_000_000:
            raise ValueError("File too large (max 5 MB)")
        args["data"] = data
    return enqueue(node["id"], "fs_" + act, v, args)


def fs_wait(c, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        with lock:
            r = results.pop(c["id"], None)
        if r:
            return r[1], r[2]
        time.sleep(0.15)
    with lock:
        c["status"] = "error"
    return False, "The node did not answer in time"


def vps_extra(vid, op, d, user):
    """edit (admin), ports (admin), backup / restore / bkdel (owner or admin)."""
    v = find(state["vps"], vid)
    admin = user["role"] == "admin"
    if not v or (not admin and v["user"] != user["id"]):
        raise LookupError("VPS not found")
    norm_vps(v)
    node = find(state["nodes"], v["node"])
    if op in ("edit", "ports") and not admin:
        raise PermissionError("Only an administrator can do this")

    if op == "ports":
        act = str(d.get("act"))
        try:
            port = int(d.get("port"))
        except (TypeError, ValueError):
            raise ValueError("Enter a port between 1 and 65535")
        if not 1 <= port <= 65535:
            raise ValueError("Enter a port between 1 and 65535")
        if act == "add":
            if any(p.get("p") == port for p in v["ports"]):
                raise ValueError("This port is already open")
            if any(o is not v and o["node"] == v["node"] and any(p.get("p") == port for p in o.get("ports", []))
                   for o in state["vps"]):
                raise ValueError("Port %d is already used by another VPS on this node" % port)
            label = re.sub(r"[^\w .-]", "", str(d.get("label") or "")).strip()[:30] or "Custom"
            v["ports"].append({"p": port, "n": label})
        elif act == "del":
            v["ports"] = [p for p in v["ports"] if p.get("p") != port]
        else:
            raise LookupError("Unknown action")
        save_state()
        return v

    if op == "edit":
        name = str(d.get("name", v["name"])).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise ValueError("VPS name: 1-32 letters, numbers, - or _")
        owner = find(state["users"], d.get("user", v["user"]))
        if not owner:
            raise ValueError("Select an owner")
        cpus = num(d, "cpus", 1, 64, v["cpus"])
        mem = num(d, "mem", 1, 1024, v["mem"])
        disk = num(d, "disk", 1, 100000, v["disk"])
        os_key = str(d.get("os") or v["os"])
        if os_key not in OS_LABELS:
            raise ValueError("Unknown operating system")
        bk_limit = num(d, "bk_limit", 0, 50, v["bk_limit"])
        if bk_limit < len(v["bk"]):
            raise ValueError("This VPS already has %d backups. Delete some first" % len(v["bk"]))
        sysd = truthy(d["sysd"]) if "sysd" in d else v["sysd"]
        os_changed = os_key != v["os"] or sysd != v["sysd"]   # either one needs a rebuilt container
        res_changed = (cpus, mem, disk) != (v["cpus"], v["mem"], v["disk"])
        if os_changed or res_changed:
            if not node or not online(node):
                raise ValueError("The node of this VPS must be online to change its OS or resources")
            if v["st"] in BUSY:
                raise ValueError("VPS is busy (%s). Wait for it to finish" % v["st"])
        old = {"cpus": v["cpus"], "mem": v["mem"], "disk": v["disk"]}
        v.update(name=name, user=owner["id"], cpus=cpus, mem=mem, disk=disk,
                 bk_limit=bk_limit)
        v["plan"] = plan_str(v)
        if os_changed:
            v["os"] = os_key
            v["sysd"] = sysd
            v["pst"] = {}
            v["st"] = "reinstalling"
            v["err"] = ""
            v["ssh"] = ""
            v["sshwait"] = False
            enqueue(node["id"], "reinstall", v, run_args(v))
        elif res_changed:
            enqueue(node["id"], "resize", v, {"cname": v["cname"], "cpus": cpus, "mem": mem, "disk": disk, "old": old})
        save_state()
        return v

    if op == "bkdel":
        b = find(v["bk"], d.get("id"))
        if not b:
            raise LookupError("Backup not found")
        if b.get("st") == "creating":
            raise ValueError("Wait until the backup is finished")
        if v["st"] == "restoring":
            raise ValueError("The VPS is restoring a backup right now")
        v["bk"].remove(b)
        if node:
            enqueue(node["id"], "bkdel", v, {"cname": v["cname"], "img": b.get("img", "")})
        save_state()
        return v

    if op in ("backup", "restore"):
        if not node or not online(node):
            raise ValueError("The node of this VPS is not online")
        if v["st"] in BUSY:
            raise ValueError("VPS is busy (%s). Wait for it to finish" % v["st"])
        if any(b.get("st") == "creating" for b in v["bk"]):
            raise ValueError("A backup is being created. Wait for it to finish")
        if op == "backup":
            if v["st"] not in ("running", "stopped"):
                raise ValueError("The VPS must be running or stopped to take a backup")
            if v["bk_limit"] <= 0:
                raise ValueError("Backups are not enabled for this VPS. Ask the administrator")
            if len(v["bk"]) >= v["bk_limit"]:
                raise ValueError("Backup limit reached: this VPS may keep %d backup%s. Delete one first"
                                 % (v["bk_limit"], "" if v["bk_limit"] == 1 else "s"))
            bid = state["nid"]
            state["nid"] += 1
            b = {"id": bid, "n": "backup-" + time.strftime("%Y%m%d-%H%M%S"), "t": time.time(), "size": 0,
                 "st": "creating", "img": "nova-bk-%d-%d" % (v["id"], bid)}
            v["bk"].append(b)
            enqueue(node["id"], "backup", v, {"cname": v["cname"], "img": b["img"], "bid": bid})
        else:
            b = find(v["bk"], d.get("id"))
            if not b or b.get("st") != "ready":
                raise ValueError("Backup not found or not ready")
            v["st"] = "restoring"
            v["err"] = ""
            v["ssh"] = ""
            v["sshwait"] = False
            a = run_args(v)
            a["img"] = b["img"]
            enqueue(node["id"], "restore", v, a)
        save_state()
        return v
    raise LookupError("Unknown action")


def edit_user(uid, d):
    u = find(state["users"], uid)
    if not u:
        raise LookupError("User not found")
    if u["id"] == 1:
        raise ValueError("The built-in admin is changed in /etc/nova-cloud/panel.env")
    name = str(d.get("username", u.get("username") or u.get("name", ""))).strip()
    email = str(d.get("email", u.get("email", ""))).strip().lower()[:80]
    pw = str(d.get("password") or "")
    role = str(d.get("role", u.get("role", "user")))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,20}", name):
        raise ValueError("Username: 3-20 letters, numbers, . - or _")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email")
    if role not in ("user", "admin"):
        raise ValueError("Role must be user or admin")
    if pw and len(pw) < 6:
        raise ValueError("Password must be at least 6 characters")
    others = [x for x in state["users"] if x is not u]
    if email == ADMIN_USER.lower() or any(str(x.get("email", "")).lower() == email for x in others):
        raise ValueError("This email is already used by another user")
    if name.lower() == ADMIN_USER.lower() or any(
            str(x.get("username") or x.get("name", "")).lower() == name.lower() for x in others):
        raise ValueError("This username is already taken")
    u["name"] = u["username"] = name
    u["email"] = email
    u["role"] = role
    if pw:
        u["pw"] = hash_pw(pw)
        for k in [k for k, (i, _) in sessions.items() if i == u["id"]]:
            del sessions[k]
        save_sessions()
        notify(u["id"], "Password changed", "An administrator changed your password.")
    state["rev"] += 1
    save_state()
    return u


def register(d):
    name = str(d.get("username", d.get("name", ""))).strip()
    email = str(d.get("email", "")).strip().lower()[:80]
    pw = str(d.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,20}", name):
        raise ValueError("Username: 3-20 letters, numbers, . - or _")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email")
    if len(pw) < 6:
        raise ValueError("Password must be at least 6 characters")
    if any(str(u.get("email", "")).lower() == email for u in state["users"]) or email == ADMIN_USER.lower():
        raise ValueError("This email is already registered")
    if any(str(u.get("username") or u.get("name", "")).lower() == name.lower() for u in state["users"]) or name.lower() == ADMIN_USER.lower():
        raise ValueError("This username is already taken")
    u = {"id": state["nid"], "name": name, "username": name, "email": email, "role": "user", "pw": hash_pw(pw)}
    state["nid"] += 1
    state["users"].append(u)
    state["rev"] += 1
    notify(u["id"], "Welcome!", "Welcome to the panel! Your account has been created.")
    save_state()
    return u


class H(BaseHTTPRequestHandler):
    server_version = "NovaCloud"

    def log_message(self, *a):
        pass

    def send_json(self, code, obj, headers=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def send_file(self, name, ctype):
        try:
            with open(os.path.join(PANEL_DIR, name), "rb") as f:
                b = f.read()
        except OSError:
            return self.send_json(404, {"error": name + " not found"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b)

    def body(self, limit=2_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def sid(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nova_sid":
                return v
        return ""

    def authed(self):
        s = self.sid()
        ses = sessions.get(s)
        if ses and ses[1] > time.time():
            with lock:
                return find(state["users"], ses[0])
        sessions.pop(s, None)
        return None

    def agent_node(self):
        auth = self.headers.get("Authorization", "")
        tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
        for n in state["nodes"]:
            if tok and hmac.compare_digest(str(n.get("token", "")), tok):
                return n
        return None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            user = self.authed()
            if not user:
                return self.send_json(401, {"error": "login required"})
            with lock:
                return self.send_json(200, view(user))
        if path in ("/", "/index.html"):
            return self.send_file("index.html", "text/html; charset=utf-8")
        if path == "/agent.py":
            return self.send_file("agent.py", "text/x-python; charset=utf-8")
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/api/login", "/api/register"):
                d = self.body(10_000)
                user = None
                if path == "/api/register":
                    with lock:
                        user = register(d)
                else:
                    name = str(d.get("username", "")).strip()
                    pw = str(d.get("password", ""))
                    with lock:
                        if hmac.compare_digest(name.encode(), ADMIN_USER.encode()) and hmac.compare_digest(pw.encode(), ADMIN_PASS.encode()):
                            user = find(state["users"], 1)
                        else:
                            for u in state["users"]:
                                if u["id"] != 1 and u.get("pw") and name.lower() in (str(u.get("email", "")).lower(), str(u.get("username") or u.get("name", "")).lower()) and check_pw(pw, u["pw"]):
                                    user = u
                    if not user:
                        time.sleep(0.6)
                        return self.send_json(401, {"error": "wrong username or password"})
                with lock:
                    user["prev"] = user.get("last")
                    user["last"] = time.time()
                    notify(user["id"], "New Login", "New login from " + (self.headers.get("X-Real-IP") or self.client_address[0]))
                    save_state()
                s = secrets.token_hex(24)
                sessions[s] = (user["id"], time.time() + SESSION_SECS)
                save_sessions()
                return self.send_json(
                    200, {"ok": True},
                    {"Set-Cookie": "nova_sid=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (s, SESSION_SECS)},
                )
            if path == "/api/logout":
                sessions.pop(self.sid(), None)
                save_sessions()
                return self.send_json(200, {"ok": True}, {"Set-Cookie": "nova_sid=; Path=/; Max-Age=0"})

            if path == "/api/agent/heartbeat":
                d = self.body(100_000)
                with lock:
                    node = self.agent_node()
                    if not node:
                        return self.send_json(401, {"error": "unknown node token"})
                    node["seen"] = time.time()
                    node["info"] = clean_info(d)
                    sweep_and_sync(node, d.get("containers"), d.get("stats"))
                    sync_fwd(node, d.get("fwd"))
                    cmds = []
                    for c in state["cmds"]:
                        if c["node"] == node["id"] and c["status"] == "pending":
                            c["status"] = "sent"
                            c["sent"] = time.time()
                            cmds.append({"id": c["id"], "op": c["op"], "args": c["args"]})
                    save_state()
                return self.send_json(200, {"ok": True, "node": node["name"],
                                            "agent_version": agent_version(), "cmds": cmds,
                                            "ports": wanted_ports(node)})
            if path == "/api/agent/progress":
                d = self.body(10_000)
                with lock:
                    node = self.agent_node()
                    if not node:
                        return self.send_json(401, {"error": "unknown node token"})
                    c = find(state["cmds"], d.get("id"))
                    v = find(state["vps"], c["vps"]) if c and c["node"] == node["id"] else None
                    if v:
                        v["step"] = str(d.get("msg", ""))[:80]
                return self.send_json(200, {"ok": True})
            if path == "/api/agent/result":
                d = self.body(9_000_000)
                with lock:
                    node = self.agent_node()
                    if not node:
                        return self.send_json(401, {"error": "unknown node token"})
                    c = find(state["cmds"], d.get("id"))
                    if not c or c["node"] != node["id"]:
                        return self.send_json(404, {"error": "unknown command"})
                    apply_result(c, bool(d.get("ok")), str(d.get("output", "")))
                    save_state()
                return self.send_json(200, {"ok": True})

            if path.startswith("/api/admin/user/"):
                user = self.authed()
                if not user or user["role"] != "admin":
                    return self.send_json(401, {"error": "login required"})
                d = self.body(10_000)
                with lock:
                    edit_user(path.rsplit("/", 1)[1], d)
                return self.send_json(200, {"ok": True})
            if path == "/api/notifs/read":
                user = self.authed()
                if not user:
                    return self.send_json(401, {"error": "login required"})
                with lock:
                    for n in state["notifs"]:
                        if n["user"] == user["id"]:
                            n["read"] = True
                    save_state()
                return self.send_json(200, {"ok": True})
            if path == "/api/vps" or path.startswith("/api/vps/"):
                user = self.authed()
                if not user:
                    return self.send_json(401, {"error": "login required"})
                parts = path.split("/")
                if len(parts) == 5 and parts[4] in ("edit", "ports", "backup", "restore", "bkdel"):
                    d = self.body(10_000)
                    with lock:
                        v = vps_extra(parts[3], parts[4], d, user)
                    return self.send_json(200, {"ok": True, "id": v["id"]})
                if len(parts) == 5 and parts[4] == "fs":
                    d = self.body(8_000_000)
                    with lock:
                        c = fs_start(user, parts[3], d)
                    ok, out = fs_wait(c)
                    if not ok:
                        return self.send_json(400, {"error": out[:300]})
                    return self.send_json(200, {"ok": True, "data": out})
                with lock:
                    if path == "/api/vps":
                        if user["role"] != "admin":
                            return self.send_json(403, {"error": "Only an administrator can create a VPS"})
                        v = create_vps(self.body(10_000))
                    else:
                        if len(parts) != 5:
                            return self.send_json(404, {"error": "not found"})
                        v = vps_op(parts[3], parts[4], user)
                return self.send_json(200, {"ok": True, "id": v["id"]})
        except PermissionError as e:
            return self.send_json(403, {"error": str(e)})
        except LookupError as e:
            return self.send_json(404, {"error": str(e)})
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except Exception:
            return self.send_json(400, {"error": "bad request"})
        self.send_json(404, {"error": "not found"})

    def do_PUT(self):
        if self.path.split("?")[0] != "/api/state":
            return self.send_json(404, {"error": "not found"})
        user = self.authed()
        if not user or user["role"] != "admin":
            return self.send_json(401, {"error": "login required"})
        try:
            d = self.body()
            users, nodes, vps = d["users"], d["nodes"], d["vps"]
            if not all(isinstance(x, list) for x in (users, nodes, vps)):
                raise ValueError
            with lock:
                if d.get("rev") != state["rev"]:
                    return self.send_json(409, {"error": "stale"})
                old_nodes = {n["id"]: n for n in state["nodes"]}
                for n in nodes:
                    n.pop("status", None)
                    o = old_nodes.get(n.get("id"))
                    n["seen"] = o.get("seen") if o else None
                    n["info"] = o.get("info") if o else None
                # VPS existence and lifecycle belong to the server
                node_ids = {n["id"] for n in nodes}
                new_vps = []
                incoming = {v.get("id"): v for v in vps if isinstance(v, dict)}
                for o in state["vps"]:
                    if o["node"] not in node_ids:
                        continue
                    v = incoming.get(o["id"])
                    if v is None:
                        new_vps.append(o)
                        continue
                    for k in SERVER_OWNED:
                        if k in o:
                            v[k] = o[k]
                        else:
                            v.pop(k, None)
                    v["node"] = o["node"]
                    new_vps.append(v)
                names, mails = set(), set()
                for u in users:
                    nm = str(u.get("username") or u.get("name", "")).lower()
                    em = str(u.get("email", "")).lower()
                    if nm in names or em in mails:
                        raise ValueError("Username or email is used by another user")
                    names.add(nm)
                    mails.add(em)
                old_users = {u["id"]: u for u in state["users"]}
                for u in users:
                    o = old_users.get(u.get("id"))
                    pw = u.pop("pw", None)
                    for k in ("last", "prev"):
                        if o and k in o:
                            u[k] = o[k]
                    if pw:
                        u["pw"] = hash_pw(str(pw))
                    elif o and o.get("pw"):
                        u["pw"] = o["pw"]
                if not any(u.get("id") == 1 for u in users):
                    users.insert(0, old_users[1])
                state["users"], state["nodes"], state["vps"] = users, nodes, new_vps
                state["nid"] = max(state["nid"], int(d.get("nid", 0)))
                state["rev"] += 1
                save_state()
            return self.send_json(200, {"ok": True, "rev": state["rev"]})
        except ValueError as e:
            return self.send_json(400, {"error": str(e) or "bad request"})
        except Exception:
            return self.send_json(400, {"error": "bad request"})


if __name__ == "__main__":
    print("Nova Cloud panel on http://%s:%d (data: %s)" % (HOST, PORT, DATA), flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
