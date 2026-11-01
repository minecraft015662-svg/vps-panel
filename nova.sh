#!/usr/bin/env bash
# Nova Cloud installer (Ubuntu / Debian)
GITHUB_USER="minecraft015662-svg"
REPO_NAME="vps-panel"
REPO="https://github.com/${GITHUB_USER}/${REPO_NAME}.git"
DIR=/opt/nova-cloud
CONF=/etc/nova-cloud
DATA=/var/lib/nova-cloud

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash nova.sh"; exit 1; }

banner() {
  clear
  echo -e "\e[1;36m"
  echo "  ╔══════════════════════════════════╗"
  echo "  ║            NOVA CLOUD            ║"
  echo "  ╚══════════════════════════════════╝"
  echo -e "\e[0m"
}

free_port() {
  systemctl stop nova-panel 2>/dev/null
  local pids
  pids=$(ss -ltnp 2>/dev/null | grep -E ':3000[[:space:]]' | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)
  [ -z "$pids" ] && return 0
  for p in $pids; do
    echo "Port 3000 is used by an OLD program: pid $p ($(ps -o args= -p "$p" | cut -c1-80))"
  done
  read -rp "Stop it so the new panel can start? (y/N): " a </dev/tty
  if [[ $a =~ ^[Yy]$ ]]; then
    for p in $pids; do kill "$p" 2>/dev/null; sleep 1; kill -9 "$p" 2>/dev/null; done
    ss -ltn 2>/dev/null | grep -qE ':3000[[:space:]]' && echo "Port 3000 is STILL busy. If it is pm2, run: pm2 list  then  pm2 delete <old panel name>"
  fi
}

find_panel() {
  SRV=$(find "$DIR" -name server.py -not -path '*/.git/*' | head -n1)
  [ -n "$SRV" ] || return 1
  PDIR=$(dirname "$SRV")
  [ -f "$PDIR/index.html" ] || return 1
}

write_service() {
  cat > /etc/systemd/system/nova-panel.service <<EOF
[Unit]
Description=Nova Cloud panel
After=network.target

[Service]
ExecStart=/usr/bin/python3 $SRV
Environment=NOVA_PANEL_DIR=$PDIR
Environment=NOVA_DATA=$DATA/data.json
EnvironmentFile=-$CONF/panel.env
Restart=always

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

install_panel() {
  echo "[1/5] Installing packages..."
  apt-get update -y && apt-get install -y nginx curl git python3 || { echo "Package installation failed"; return; }
  echo "[2/5] Downloading the panel..."
  rm -rf "$DIR"
  git clone --depth 1 "$REPO" "$DIR" || { echo "Repo clone failed. Check GITHUB_USER and that the repo is public."; return; }
  SRV=$(find "$DIR" -name server.py -not -path '*/.git/*' | head -n1)
  [ -n "$SRV" ] || { echo "panel/server.py not found in the repo. Upload the whole 'panel' folder (index.html + server.py)."; return; }
  PDIR=$(dirname "$SRV")
  [ -f "$PDIR/index.html" ] || { echo "index.html not found next to server.py ($PDIR)."; return; }
  chmod -R a+rX "$DIR"
  [ -f "$DIR/nova.sh" ] && install -m 755 "$DIR/nova.sh" /usr/local/bin/nova
  echo "[3/5] Starting the panel service..."
  mkdir -p "$CONF" "$DATA"
  if [ ! -f "$CONF/panel.env" ]; then
    printf 'NOVA_ADMIN_USER=admin\nNOVA_ADMIN_PASS=admin\n' > "$CONF/panel.env"
    chmod 600 "$CONF/panel.env"
  fi
  write_service
  systemctl daemon-reload
  systemctl enable nova-panel >/dev/null 2>&1
  free_port
  systemctl restart nova-panel
  echo "[4/5] Nginx setup..."
  cat > /etc/nginx/sites-available/nova-cloud <<'EOF'
server {
  listen 80 default_server;
  listen [::]:80 default_server;
  server_name _;
  client_max_body_size 2m;
  location / {
    proxy_pass http://127.0.0.1:3000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
  }
}
EOF
  ln -sf /etc/nginx/sites-available/nova-cloud /etc/nginx/sites-enabled/nova-cloud
  rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf
  nginx -t || { echo "Nginx config test failed"; return; }
  systemctl enable nginx >/dev/null 2>&1
  systemctl restart nginx
  command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active" && ufw allow 80/tcp >/dev/null
  echo "[5/5] Checking the panel..."
  sleep 2
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/)
  if [ "$code" = "200" ]; then
    echo "Done. Panel: http://$(curl -fsS ifconfig.me 2>/dev/null || echo SERVER_IP)  (login: admin / admin)"
    echo "Change the login: edit $CONF/panel.env then run: systemctl restart nova-panel"
    echo "Next time just type: sudo nova   (menu has Update)"
  else
    echo "Panel check failed (HTTP $code). Logs:"
    journalctl -u nova-panel -n 8 --no-pager 2>/dev/null
    tail -n 5 /var/log/nginx/error.log 2>/dev/null
  fi
}

uninstall_panel() {
  read -rp "Remove Nova Cloud completely? (y/N): " a </dev/tty
  [[ $a =~ ^[Yy]$ ]] || { echo "Cancel."; return; }
  systemctl disable --now nova-panel 2>/dev/null || true
  systemctl disable --now nova-node 2>/dev/null || true
  rm -f /etc/systemd/system/nova-panel.service /etc/systemd/system/nova-node.service /usr/local/bin/nova-agent /usr/local/bin/nova-agent.py /usr/local/bin/nova
  rm -f /etc/nginx/sites-enabled/nova-cloud /etc/nginx/sites-available/nova-cloud
  systemctl reload nginx 2>/dev/null || true
  systemctl daemon-reload
  rm -rf "$DIR" "$CONF" "$DATA"
  echo "Nova Cloud has been removed."
}

install_agent() {
  curl -fsS -m 20 "$1/agent.py" -o /usr/local/bin/nova-agent.py.new || { echo "Could not download the agent from $1/agent.py (is the panel updated and running?)"; return 1; }
  python3 -m py_compile /usr/local/bin/nova-agent.py.new || { echo "Downloaded agent is invalid"; rm -f /usr/local/bin/nova-agent.py.new; return 1; }
  mv /usr/local/bin/nova-agent.py.new /usr/local/bin/nova-agent.py
  chmod +x /usr/local/bin/nova-agent.py
  rm -f /usr/local/bin/nova-agent
  cat > /etc/systemd/system/nova-node.service <<'EOF'
[Unit]
Description=Nova Cloud node agent
After=network-online.target docker.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/nova-agent.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable nova-node >/dev/null 2>&1
  systemctl restart nova-node
}

update_panel() {
  [ -d "$DIR/.git" ] || { echo "Panel is not installed. Choose 1 (Install) first."; return; }
  echo "Downloading the latest version from $REPO (data and login are kept)..."
  git -C "$DIR" remote set-url origin "$REPO"
  git -C "$DIR" fetch --depth 1 origin main && git -C "$DIR" reset --hard FETCH_HEAD || { echo "Update failed (check internet and that the repo is public)."; return; }
  find_panel || { echo "server.py + index.html not found in the repo."; return; }
  chmod -R a+rX "$DIR"
  [ -f "$DIR/nova.sh" ] && install -m 755 "$DIR/nova.sh" /usr/local/bin/nova
  mkdir -p "$CONF" "$DATA"
  write_service
  systemctl enable nova-panel >/dev/null 2>&1
  free_port
  systemctl restart nova-panel
  sleep 2
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/)
  if [ "$code" = "200" ]; then
    echo "Panel updated and running from: $PDIR"
    curl -s http://127.0.0.1:3000/ | grep -q 'id="sb"' && echo "UI check: new design is live." || echo "UI check: still the OLD index.html. Check the file in GitHub."
    echo "Nodes update their agent automatically within a minute."
  else
    echo "Panel check failed (HTTP $code). Logs:"
    journalctl -u nova-panel -n 10 --no-pager 2>/dev/null
  fi
}

update_node() {
  [ -f "$CONF/node.conf" ] || { echo "This server is not set up as a node. Choose 3 first."; return; }
  . "$CONF/node.conf"
  install_agent "$PANEL_URL" && echo "Node agent updated and restarted."
}

node_setup() {
  URL="${1:-}"; TOKEN="${2:-}"
  [ -n "$URL" ] || read -rp "Panel URL (e.g. http://1.2.3.4): " URL </dev/tty
  [ -n "$TOKEN" ] || read -rp "Node token: " TOKEN </dev/tty
  URL="${URL%/}"
  [ -n "$URL" ] && [ -n "$TOKEN" ] || { echo "Both URL and token are required."; return; }
  apt-get update -y && apt-get install -y curl python3 docker.io qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils lxcfs || { echo "Package installation failed"; return; }
  systemctl enable --now docker
  systemctl enable --now libvirtd 2>/dev/null || true
  [ -e /dev/kvm ] || echo "Warning: /dev/kvm not found. KVM needs hardware virtualization. Docker VPS will still work."
  umask 077
  mkdir -p "$CONF"
  printf 'PANEL_URL=%s\nTOKEN=%s\n' "$URL" "$TOKEN" > "$CONF/node.conf"
  umask 022
  install_agent "$URL" || return
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 8 -X POST "$URL/api/agent/heartbeat" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}')
  case "$code" in
    200) echo "Node connected to the panel. It shows 'online' within a few seconds." ;;
    401) echo "The panel rejected the token. Copy the token again from Admin panel > Nodes." ;;
    000) echo "Cannot reach $URL. Check the panel URL, firewall and that the panel is running." ;;
    *)   echo "Unexpected answer from the panel (HTTP $code)." ;;
  esac
  echo "Agent service: systemctl status nova-node"
  echo "Docker VPS that run Pterodactyl need the Sysbox runtime (github.com/nestybox/sysbox)."
}

case "$1" in
  panel) if [ -d "$DIR/.git" ]; then update_panel; else install_panel; fi; exit 0 ;;
  node) node_setup "$2" "$3"; exit 0 ;;
  update) update_panel; exit 0 ;;
  node-update) update_node; exit 0 ;;
esac

while true; do
  banner
  echo "  1) Install"
  echo "  2) Uninstall"
  echo "  3) Node setup"
  echo "  4) Update panel (keeps data)"
  echo "  5) Update node agent"
  echo "  0) Exit"
  echo
  read -rp "  Choose an option [0-5]: " c </dev/tty
  echo
  case "$c" in
    1) install_panel ;;
    2) uninstall_panel ;;
    3) node_setup ;;
    4) update_panel ;;
    5) update_node ;;
    0) exit 0 ;;
    *) echo "Invalid option." ;;
  esac
  echo
  read -rp "Press Enter to return to the menu..." _ </dev/tty
done
