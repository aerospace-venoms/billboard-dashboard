#!/usr/bin/env python3
"""Create a private deployment configuration. No credentials are printed."""
import argparse
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--non-interactive', action='store_true')
    p.add_argument('--server-ip', help='IPv4 address of the Docker host')
    p.add_argument('--port', type=int, default=8090)
    p.add_argument('--allow', action='append', help='Allowed client IP/CIDR; repeat as needed')
    p.add_argument('--home-lat', type=float)
    p.add_argument('--home-lon', type=float)
    p.add_argument('--work-lat', type=float)
    p.add_argument('--work-lon', type=float)
    p.add_argument('--timezone')
    p.add_argument('--label', default='DASHBOARD')
    p.add_argument('--region', default='')
    p.add_argument('--viewer', default='viewer')
    p.add_argument('--no-map', action='store_true')
    return p


def collect(args):
    ssid, password = 'CHANGE_ME', 'CHANGE_ME'  # pragma: allowlist secret
    if not args.non_interactive:
        args.server_ip = args.server_ip or input('Docker host IPv4 address: ').strip()
        args.allow = args.allow or input('Allowed client IPs/CIDRs (comma separated; include badge): ').split(',')
        if args.home_lat is None: args.home_lat = float(input('Home latitude: '))
        if args.home_lon is None: args.home_lon = float(input('Home longitude: '))
        args.timezone = args.timezone or input('IANA timezone (e.g. Europe/London): ').strip()
        if input('Configure a commute? [y/N] ').lower() == 'y':
            args.work_lat = float(input('Destination latitude: '))
            args.work_lon = float(input('Destination longitude: '))
        args.label = input('Display heading [DASHBOARD]: ').strip() or args.label
        args.region = input('Display subheading [blank]: ').strip()
        ssid = input('Badge Wi-Fi SSID (blank to configure later): ') or 'CHANGE_ME'
        if ssid != 'CHANGE_ME': password = getpass.getpass('Badge Wi-Fi password: ')
    args.server_ip = str(ipaddress.IPv4Address(args.server_ip or ''))
    if not 1 <= args.port <= 65535: raise ValueError('Invalid port')
    if not args.allow: raise ValueError('At least one --allow IP/CIDR is required')
    args.allow = [str(ipaddress.IPv4Network(item.strip(), strict=False)) for item in args.allow]
    ZoneInfo(args.timezone or '')
    for lat, lon in [(args.home_lat, args.home_lon), (args.work_lat, args.work_lon)]:
        if lat is None and lon is None and (lat, lon) != (args.home_lat, args.home_lon): continue
        if lat is None or lon is None or not (-85 <= lat <= 85 and -180 <= lon <= 180):
            raise ValueError('Coordinates must be a complete latitude/longitude pair in range')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', args.viewer): raise ValueError('Invalid viewer name')
    for value in [args.label, args.region, ssid, password]:
        if any(ord(c) < 32 for c in value): raise ValueError('Control characters are not supported')
    if any("'" in v or '\\' in v for v in [args.label, args.region]):
        raise ValueError('Quotes/backslashes are not supported in display labels')
    if len(ssid.encode()) > 32 or len(password.encode()) > 63: raise ValueError('Wi-Fi credential too long')
    return ssid, password


def provision(args, root=ROOT):
    ssid, wifi_password = collect(args)
    if not shutil.which('openssl'): raise ValueError('Install openssl first')
    targets = ['.env', 'secrets', 'generated', 'firmware/main/private_config.h', 'firmware/main/dashboard_ca.h']
    if any((root/p).exists() for p in targets):
        raise ValueError('Configuration already exists; refusing to overwrite private material')
    os.umask(0o077)
    private = root/'secrets'
    (private/'ca-private').mkdir(parents=True)
    (private/'tls').mkdir()
    (root/'generated').mkdir()
    url = f'https://{args.server_ip}:{args.port}'
    ca, tls = private/'ca-private', private/'tls'
    def openssl(*cmd):
        subprocess.run(['openssl', *map(str, cmd)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
            '-keyout', ca/'ca.key', '-out', root/'generated/dashboard-ca.crt', '-days', '3650',
            '-subj', '/CN=Billboard Dashboard Private CA', '-addext', 'basicConstraints=critical,CA:TRUE',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
            '-keyout', tls/'server.key', '-out', tls/'server.csr', '-subj', '/CN=Billboard Dashboard')
    ext = tls/'extensions.cnf'
    ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
                   'extendedKeyUsage=serverAuth\nsubjectAltName=IP:'+args.server_ip+'\n')
    openssl('x509', '-req', '-in', tls/'server.csr', '-CA', root/'generated/dashboard-ca.crt',
            '-CAkey', ca/'ca.key', '-set_serial', str(secrets.randbits(128)), '-days', '825',
            '-extfile', ext, '-out', tls/'server.crt')
    (tls/'server.csr').unlink(); ext.unlink()
    token, password = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    (private/'badge-1.token').write_text(token+'\n')
    (private/'viewer-login.txt').write_text(f'URL: {url}/\nUsername: {args.viewer}\nPassword: {password}\n')
    (private/'auth.json').write_text(json.dumps({'devices': {'badge-1': hashlib.sha256(token.encode()).hexdigest()},
        'viewers': {args.viewer: hashlib.sha256(password.encode()).hexdigest()}}, indent=2)+'\n')
    (private/'tomtom_key').touch(mode=0o600)
    (root/'generated/allowed-clients.conf').write_text(''.join(f'allow {network};\n' for network in args.allow))
    settings = dict(BIND_ADDRESS=args.server_ip, PORT=args.port, TZ=args.timezone, HOME_LAT=args.home_lat,
        HOME_LON=args.home_lon, WORK_LAT=args.work_lat if args.work_lat is not None else '',
        WORK_LON=args.work_lon if args.work_lon is not None else '', LOCATION_LABEL=args.label,
        REGION_LABEL=args.region, REFRESH_SECONDS=3600, MAP_ENABLED='false' if args.no_map else 'true',
        BRIDGE_PREFIX='br-bboard')
    # Single-quoted dotenv values avoid Compose interpolation of '$'.
    if any("'" in str(v) or '\\' in str(v) for v in settings.values()):
        raise ValueError('Quotes/backslashes are not supported in display labels')
    (root/'.env').write_text(''.join(f"{k}='{v}'\n" for k,v in settings.items()))
    values = dict(WIFI_SSID=ssid, WIFI_PASSWORD=wifi_password, DASHBOARD_BASE=url,
                  DASHBOARD_TOKEN=token, NTP_SERVER='pool.ntp.org')
    (root/'firmware/main/private_config.h').write_text('#pragma once\n'+''.join(
        f'#define {k} {json.dumps(v)}\n' for k,v in values.items())+'#define TEST_SLEEP_SECONDS 0\n')
    cert = (root/'generated/dashboard-ca.crt').read_text()
    (root/'firmware/main/dashboard_ca.h').write_text('#pragma once\nstatic const char dashboard_ca[] =\n'+
        ''.join(json.dumps(line+'\n')+'\n' for line in cert.splitlines())+';\n')
    # Public configuration/certificates must be readable by non-root containers.
    for p in [root/'generated', private/'tls']: p.chmod(0o755)
    for p in [root/'generated/allowed-clients.conf', root/'generated/dashboard-ca.crt', tls/'server.crt']:
        p.chmod(0o644)


def main():
    try: provision(parser().parse_args())
    except (ValueError, OSError) as exc: raise SystemExit(str(exc))
    print('Created private configuration. See secrets/viewer-login.txt for the browser login.')
    print('Run sudo scripts/install-permissions.sh before Docker Compose. Keep secrets and generated files private.')

if __name__ == '__main__': main()
