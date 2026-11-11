#!/bin/sh
# On the Docker host, after provisioning; never mount the entire secrets folder.
set -eu
cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo 'Run with sudo on the Docker host.' >&2; exit 1; }
for file in secrets/auth.json secrets/tomtom_key secrets/tls/server.key; do
    [ -f "$file" ] || { echo 'Run scripts/setup.py first.' >&2; exit 1; }
done
chown 10001:10001 secrets/auth.json secrets/tomtom_key
chmod 400 secrets/auth.json secrets/tomtom_key
chown 101:101 secrets/tls/server.key
chmod 400 secrets/tls/server.key
chmod 644 secrets/tls/server.crt generated/dashboard-ca.crt generated/allowed-clients.conf
