# Security and deployment

This is a private-network application. Do not expose it directly to the Internet.

## Trust boundaries

The browser and badge connect to nginx over TLS 1.2/1.3. nginx checks a client-IP
allowlist and applies connection/request limits. The Python service authenticates
all external reads with separate viewer/device credentials. Only devices can
submit battery telemetry; generated high-entropy secrets are stored as SHA-256
digests on the server. Do not substitute short, human-chosen passwords.

The renderer has no published port or default external route. It uses two internal
Docker bridges in isolated gateway mode: one to nginx, one to Squid. The proxy
permits HTTPS CONNECT only to the configured weather, imagery and routing hosts,
and rejects private destinations. Python verifies upstream TLS independently.
All services run without root, drop capabilities, disallow privilege escalation,
use read-only filesystems, and have memory/CPU/PID limits. Only the renderer can
read the TomTom key/auth digest file; only nginx can read its TLS private key.

These controls reduce risk, not eliminate it. A compromised renderer can read
its mounted secrets and alter cached output. A kernel/container escape could
exceed those restrictions. Keep Docker, host packages, Python/image dependencies
and ESP-IDF maintained. No automatic host upgrades are installed.

## Additional host firewall (native Linux, root required)

An internal renderer network does not by itself restrict a compromised frontend
or egress proxy. The optional host rules block new frontend connections outside
its backend network; the proxy can reach public HTTPS/DNS but not private LAN or
host services. They operate on bridge interfaces `br-bboard-in` and
`br-bboard-out`, so `BRIDGE_PREFIX` must remain `br-bboard` unless you also
edit the script. One such stack per Docker host is supported by the stock rules.

Review `security/host-firewall.sh` before installing. It uses the **iptables**
Docker backend and separate `BILLBOARD-OUT`/`BILLBOARD-HOST` chains. It never flushes
unrelated chains. Native nftables Docker backends need an equivalent policy;
do not assume this script protects them.

After Docker is installed and running:

```sh
sudo install -m 0755 security/host-firewall.sh /usr/local/sbin/billboard-firewall
sudo install -m 0644 security/billboard-firewall.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now billboard-firewall.service
sudo systemctl status billboard-firewall.service
sudo iptables -S BILLBOARD-OUT
```

Install these before starting the dashboard when possible. The unit reapplies
rules after Docker starts/restarts; it is not a continuous firewall reconciler.
If another firewall manager removes the chains, reapply and verify them. Do not
restart Docker casually on hosts running unrelated containers.

To uninstall **only this project's rules**, first stop the dashboard, then:

```sh
sudo systemctl disable --now billboard-firewall.service
sudo iptables -D DOCKER-USER -j BILLBOARD-OUT
sudo iptables -D INPUT -j BILLBOARD-HOST
sudo iptables -F BILLBOARD-OUT
sudo iptables -X BILLBOARD-OUT
sudo iptables -F BILLBOARD-HOST
sudo iptables -X BILLBOARD-HOST
sudo rm /etc/systemd/system/billboard-firewall.service /usr/local/sbin/billboard-firewall
sudo systemctl daemon-reload
```

## Certificates and credentials

The setup wizard creates a dedicated private CA, an 825-day server certificate,
a random 256-bit badge token and a random 192-bit viewer password. Protect and
back up the CA private key on the provisioning computer; it does not belong on
the server. Import only `generated/dashboard-ca.crt` into browser trust stores.
The server certificate has the configured server IPv4 address as a subject
alternative name; changing the server address requires reissuing it.

Check expiry with:

```sh
openssl x509 -in secrets/tls/server.crt -noout -enddate
```

Renew before expiration, using the same CA and a SAN matching the server address.
Keep the new private key readable only by UID 101, replace the certificate/key,
and recreate `frontend`. Changing the CA requires updating browser trust and
rebuilding/reflashing badges. There is no automatic certificate renewal yet.

For credential rotation, create a fresh random secret, update its SHA-256 digest
in `secrets/auth.json`, and recreate the renderer/frontend. To revoke a badge,
remove its device entry. Use a separate token for each badge. Keep raw tokens out
of command-line arguments, logs, issue reports and URLs. Files owned by container
UIDs can be edited using `sudoedit` on the Docker host; rerun the permissions
script afterwards. Recreating containers is needed when atomically replacing
bind-mounted files:

```sh
sudo scripts/install-permissions.sh
docker compose up -d --force-recreate dashboard frontend
```

TLS does not protect credentials extracted physically from badge flash. Secure
boot and flash encryption are not enabled by this project. Its unauthenticated
NTP setup also has the time-manipulation limitations described in the firmware
README. The badge retains its image on connection failures; inspect its update
timestamp rather than assuming the visible screen is fresh.

## Publishing and support requests

Never publish `.env`, `secrets/`, `generated/`, caches, provisioning files, firmware
binaries or build trees. Screenshots can reveal your location, routine or battery
state. Use `scripts/demo.py` to create a fictional example instead. Logs and Git
author metadata also deserve review. Adding a file to `.gitignore` does not remove
it from existing history; rotate any credential that was exposed.

Run `python3 scripts/check_public.py` on staged files before commits. The check
rejects private paths and common sensitive artifacts; it is not a substitute for
review or a full secret scanner. Tests use explicit fake credentials. CI does not
upload firmware artifacts, private setup output or live dashboard captures.
