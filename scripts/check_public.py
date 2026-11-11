#!/usr/bin/env python3
"""Check the Git index for forbidden deployment files and private-key material."""
from pathlib import PurePosixPath
import re
import subprocess
import sys


def main():
    names=subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
    failures=[]
    for name in filter(None,names):
        path=PurePosixPath(name)
        forbidden=(any(part in {'secrets','generated','data','build','.venv','__pycache__'} for part in path.parts)
                   or path.name in {'private_config.h','dashboard_ca.h','sdkconfig','sdkconfig.old'}
                   or (path.name.startswith('.env') and path.name != '.env.example')
                   or path.suffix in {'.key','.pem','.crt','.bin','.elf','.map','.pyc'})
        if forbidden: failures.append(name+': private/generated path')
        body=subprocess.check_output(['git','show',':'+name])
        if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',body):
            failures.append(name+': private key')
        # Only the reviewed public-location demo is an allowed tracked raster image.
        if path.suffix.lower() in {'.png','.jpg','.jpeg','.webp'} and name != 'docs/demo.png':
            failures.append(name+': unexpected raster image; review for personal data')
    if failures:
        print('\n'.join(failures),file=sys.stderr)
        return 1
    print('Public-file check passed (Git index; no private/generated artifacts).')
    return 0

if __name__ == '__main__': raise SystemExit(main())
