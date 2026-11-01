# Nova Cloud

Your own hosting control panel (vPanel).

## Repo structure (upload exactly like this)

```
vps-panel/
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
curl -fsSL https://raw.githubusercontent.com/minecraft015662-svg/vps-panel/main/nova.sh -o nova.sh
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
- **Admin > Users > Edit**: change username, email, password and role of a user. Changing the password signs that user out.
- **systemd VPS**: every new VPS runs systemd as PID 1 (privileged, with its own `/var/lib/docker` volume). `systemctl`, `apt`/`dnf`, Docker inside the VPS, and a full Pterodactyl Panel + Wings all work. VPS created before agent v5 have no systemd: use Reinstall once. The first VPS of each OS builds a cached systemd image on the node (1-3 minutes), the next ones are fast. Backups are `docker commit` images and do not include the inside-Docker data volume.
- **Live usage**: the dashboard cards and the Manage page show real CPU and RAM of the container and refresh every 2 seconds.
- **VPS settings (Admin > VPS > Edit, or Manage > Settings)**: name, owner, CPU cores, RAM, disk, operating system (Ubuntu 20.04/22.04/24.04, Debian 11/12/13, Rocky Linux 9, AlmaLinux 9), backups allowed and ports allowed. CPU and RAM are applied to the running VPS without data loss. Changing the OS reinstalls the VPS.
- **Ports**: every VPS gets only as many ports as the admin allows (default 5). The Ports tab shows `used / allowed`. Ports are still records in the panel, they do not open anything on the node yet.
- **Backups**: every VPS may keep only as many backups as the admin allows (default 2). A backup is a Docker image committed from the VPS. Create, Restore and Delete work from the Backups tab (owner or admin).
- Files, Terminal and Subusers: Files and Terminal are real, Subusers are still only records. KVM VPS are not supported yet (the type is shown as "coming soon").
- The panel runs over plain HTTP. Put it behind HTTPS before using real passwords.

## Updating

Upload all 5 files to your GitHub repo, then on the panel server run `sudo nova` and choose 4 (Update panel). Nodes download the new agent by themselves within a few seconds (agent version 5).
