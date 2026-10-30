#!/bin/bash
# VPSForge Panel Uninstaller

set -e

echo "==================================="
echo "  VPSForge Panel Uninstaller"
echo "==================================="

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo)"
  exit 1
fi

echo "Stopping services..."
systemctl stop nginx || true
systemctl stop docker || true

echo "Removing panel files..."
rm -rf /opt/vpsforge

echo "Removing Nginx config..."
rm -f /etc/nginx/sites-enabled/vpsforge
rm -f /etc/nginx/sites-available/vpsforge
systemctl restart nginx || true

echo "Uninstall complete. Docker and LXC packages left installed (remove manually if needed)."
echo "==================================="
