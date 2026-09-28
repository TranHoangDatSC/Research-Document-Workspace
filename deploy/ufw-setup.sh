#!/usr/bin/env bash
# Idempotent UFW baseline for the VPS: allow SSH + HTTP/HTTPS only, deny the
# rest. Run once on the VPS as root/sudo, after confirming SSH access works
# (never enable ufw before confirming the SSH rule, or you can lock yourself
# out).
#
# PostgreSQL/MongoDB/MinIO are already unreachable from outside the VPS
# because docker-compose.yaml only publishes them on 127.0.0.1; this firewall
# is defense in depth, not the only thing standing between them and the
# internet.
set -euo pipefail

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose
