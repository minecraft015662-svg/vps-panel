# Nova Cloud

Your own hosting control panel (vPanel).

## Repo structure (upload exactly like this)

```
Nova-cloud/
├── README.md
├── nova.sh            # installer menu
└── panel/
    ├── index.html     # panel UI
    ├── server.py      # panel backend (Python 3, no extra packages)
    └── agent.py       # node agent (served to nodes by the panel)
```

A flat layout also works: put all 4 files (`index.html`, `server.py`, `agent.py`, `nova.sh`) in the repo root.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/minecraft015662-svg/Nova-cloud/main/nova.sh -o nova.sh
sudo bash nova.sh
```

Menu:

1. Install: installs nginx + python3, starts the `nova-panel` service, checks the panel
2. Uninstall: removes everything
3. Node setup: installs Docker, KVM, lxcfs and the node agent (`nova-node` service)
4. Update panel: pulls the latest files from GitHub and restarts the panel. Data and login are kept.
5. Update node agent: re-downloads the agent from the panel (nodes also update it automatically)

After the first install you can open the menu with `sudo nova`.

Login: `admin` / `admin`. Change it in `/etc/nova-cloud/panel.env`, then `systemctl restart nova-panel`.

## Connect a node

1. Panel: Admin panel > Nodes > Create New. The token appears in the list.
2. On the node VPS: `sudo bash nova.sh`, choose 3, enter the panel URL (e.g. `http://1.2.3.4`) and the token.
3. The node shows `online` within seconds. It goes `offline` if the agent stops for 30 seconds.

## Status

- Users, nodes and VPS are saved on the server (`/var/lib/nova-cloud/data.json`).
- Nodes connect for real (agent heartbeat with token).
- Docker VPS are real: create, Start, Stop, Restart, Reinstall, Delete, and an sshx terminal link. A new VPS is created and left running only after the node finishes installing it.
- Files, Ports, Subusers and Backups tabs inside a VPS are still only records in the panel. KVM VPS are not supported yet.
- The panel runs over plain HTTP. Put it behind HTTPS before using real passwords.
