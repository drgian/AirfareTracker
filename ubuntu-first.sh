#!/bin/bash
# FlightFare: first command to run on the freshly installed Ubuntu machine.
# Installs the SSH server and authorises Claude's setup key, so the rest can be done remotely.
set -e
echo "Installing the SSH server (you'll be asked for your password)..."
sudo apt-get update -qq
sudo apt-get install -y -qq openssh-server
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEx8up9U2uYJok+zu8w5/LjSXwKqVe+SkjPScRKLdgxc flight-tracker-setup"
grep -qxF "$KEY" "$HOME/.ssh/authorized_keys" 2>/dev/null || echo "$KEY" >> "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
sudo systemctl enable --now ssh
echo
echo "  user:     $USER"
echo "  hostname: $(hostname)"
echo "  address:  $(hostname -I | awk '{print $1}')"
echo "  ssh:      $(systemctl is-active ssh)"
echo
echo "Send Claude the address above."
