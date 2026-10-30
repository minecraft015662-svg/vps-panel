# VPSForge - Custom VPS Management Panel

A modern, multi-node VPS control panel supporting KVM, LXC, Docker, LVM, and HVM.

## Features
- Multi-node management (Local + Remote VPS)
- Create KVM / LXC / Docker VPS instances
- User management with SSH access & passwords
- Built-in file manager
- Modern English UI
- Easy install / uninstall scripts
- Deployable on Render (web frontend) + agents on nodes

## Quick Start (On a Linux VPS Node)

```bash
# Make scripts executable
chmod +x scripts/*.sh

# Install
sudo bash scripts/install.sh

# Uninstall
sudo bash scripts/uninstall.sh
```

## Deploy Web Panel on Render
1. Push this folder to a GitHub repo
2. Create a new Web Service on Render
3. Build command: `npm install`
4. Start command: `node server.js`
5. Or use Static Site for the `public/` folder

## Architecture
- **Web Panel**: Deploy on Render (or any host)
- **Agents**: Install on each VPS node via the install script
- Supports adding remote nodes by IP + SSH credentials

## Supported Virtualization
- **KVM / HVM / HVM9**: Full virtual machines (requires libvirt on node)
- **LXC**: Lightweight containers
- **Docker**: Container-based VPS
- **LVM**: Storage management for disks

## Default Credentials
- Username: `admin`
- Password: `admin123`

**Important**: Change the password immediately after first login.

## Node Setup
1. First node is created automatically from the local server where the panel is installed.
2. To add more nodes: Go to Nodes → Add Remote Node → Enter IP, SSH port, username, and password/key.

## Notes
This is a starter template. Full production KVM/libvirt integration requires additional packages (`libvirt-daemon-system`, `virt-manager` etc.) on the nodes. Docker and LXC work out of the box after running the installer.
