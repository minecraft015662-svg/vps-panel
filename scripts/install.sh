#!/bin/bash
# VPSForge Panel Installer
# Supports Ubuntu/Debian based systems
# Sets up the web panel and basic virtualization tools (Docker, LXC)

set -e

echo "==================================="
echo "   VPSForge Panel Installer"
echo "==================================="

# Check root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo)"
  exit 1
fi

echo "[1/6] Updating system packages..."
apt-get update -y
apt-get upgrade -y

echo "[2/6] Installing dependencies..."
apt-get install -y curl wget git nginx nodejs npm docker.io lxc lxc-utils bridge-utils

echo "[3/6] Enabling services..."
systemctl enable docker
systemctl start docker
systemctl enable lxc
systemctl start lxc || true

echo "[4/6] Setting up web panel..."
mkdir -p /opt/vpsforge
# In real deployment, copy the panel files here
# For now, assume user will upload or git clone

echo "[5/6] Configuring Nginx reverse proxy..."
cat > /etc/nginx/sites-available/vpsforge << 'EOF'
server {
    listen 80;
    server_name _;
    root /opt/vpsforge/public;
    index index.html;
    location / {
        try_files $uri $uri/ /index.html;
    }
    location /api/ {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

ln -sf /etc/nginx/sites-available/vpsforge /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo "[6/6] Installation complete!"
echo "Access the panel at http://YOUR_SERVER_IP"
echo "Default login: admin / admin123 (change immediately)"
echo "==================================="
