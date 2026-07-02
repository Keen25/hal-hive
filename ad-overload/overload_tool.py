#!/usr/bin/env python3
"""
HAL — AD Overload Seeder
Generates LDIF for 90,000 AD objects (30k users, 25k groups, 35k computers)
on HAL, then imports via ldifde on the DC.

Validated: ~200 obj/s, ~7m 30s end-to-end on fodor.monster.local.

Usage:
  python3 overload_tool.py generate              # write objects.ldif
  python3 overload_tool.py import <dc_ip>        # push ldif to DC + import
  python3 overload_tool.py run <dc_ip>           # generate + import
"""

import os
import sys
import time
import subprocess
import winrm
import base64
import textwrap

# ── Config ────────────────────────────────────────────────────────────────────

DOMAIN_DN  = 'DC=fodor,DC=monster,DC=local'
DOMAIN     = 'fodor.monster.local'
OU_BASE    = f'OU=HiveLoad,{DOMAIN_DN}'
LDIF_PATH  = os.path.expanduser('~/hive/ad-overload/objects.ldif')

DC_CONFIG = {
    'host':      '192.168.100.10',
    'transport': 'kerberos',
    'username':  r'fodor\a',
    'password':  os.environ.get('LAB_PASS'),
}
if not DC_CONFIG['password']:
    raise SystemExit('LAB_PASS not set — source ~/.hive/hive.env')

COUNTS = {
    'users':     30_000,
    'groups':    25_000,
    'computers': 35_000,
}

# ── LDIF generation ───────────────────────────────────────────────────────────

def _ou_entry(name: str, parent: str) -> str:
    dn = f'OU={name},{parent}'
    return (
        f'dn: {dn}\n'
        f'changetype: add\n'
        f'objectClass: organizationalUnit\n'
        f'ou: {name}\n\n'
    )


def _user_entry(i: int) -> str:
    name = f'hiveuser{i:06d}'
    dn   = f'CN={name},OU=Users,{OU_BASE}'
    return (
        f'dn: {dn}\n'
        f'changetype: add\n'
        f'objectClass: user\n'
        f'sAMAccountName: {name}\n'
        f'givenName: Hive\n'
        f'sn: User{i:06d}\n'
        f'userPrincipalName: {name}@{DOMAIN}\n'
        f'userAccountControl: 514\n\n'  # disabled — no password complexity needed
    )


def _group_entry(i: int) -> str:
    name = f'hivegroup{i:06d}'
    dn   = f'CN={name},OU=Groups,{OU_BASE}'
    return (
        f'dn: {dn}\n'
        f'changetype: add\n'
        f'objectClass: group\n'
        f'sAMAccountName: {name}\n'
        f'groupType: -2147483646\n\n'  # global security group
    )


def _computer_entry(i: int) -> str:
    name = f'HIVEPC{i:06d}'
    dn   = f'CN={name},OU=Computers,{OU_BASE}'
    return (
        f'dn: {dn}\n'
        f'changetype: add\n'
        f'objectClass: computer\n'
        f'sAMAccountName: {name}$\n'
        f'userAccountControl: 4096\n\n'  # workstation trust account
    )


def generate(output_path: str = LDIF_PATH) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    t0 = time.time()
    total = sum(COUNTS.values())
    print(f'Generating LDIF: {total:,} objects → {output_path}')

    with open(output_path, 'w') as f:
        # Container OUs
        f.write(_ou_entry('HiveLoad', DOMAIN_DN))
        f.write(_ou_entry('Users',     OU_BASE))
        f.write(_ou_entry('Groups',    OU_BASE))
        f.write(_ou_entry('Computers', OU_BASE))

        for i in range(COUNTS['users']):
            f.write(_user_entry(i))
        for i in range(COUNTS['groups']):
            f.write(_group_entry(i))
        for i in range(COUNTS['computers']):
            f.write(_computer_entry(i))

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    elapsed = time.time() - t0
    print(f'Done: {size_mb:.1f} MB in {elapsed:.1f}s')


# ── DC import via WinRM ───────────────────────────────────────────────────────

def _winrm_session(host: str) -> winrm.Session:
    return winrm.Session(
        f'http://{host}:5985/wsman',
        auth=(DC_CONFIG['username'], DC_CONFIG['password']),
        transport=DC_CONFIG['transport'],
        read_timeout_sec=1800,
        operation_timeout_sec=1800,
    )


def _scp_ldif(ldif_path: str, dc_ip: str, remote_path: str = r'C:\hive\objects.ldif') -> None:
    """Copy LDIF to DC via SCP (OpenSSH must be enabled on DC)."""
    print(f'Copying LDIF to {dc_ip}:{remote_path} ...')
    subprocess.run(
        ['scp', '-o', 'StrictHostKeyChecking=no',
         ldif_path, f'a@{dc_ip}:{remote_path}'],
        check=True,
    )


def import_ldif(dc_ip: str, ldif_path: str = LDIF_PATH) -> None:
    remote_ldif = r'C:\hive\objects.ldif'
    _scp_ldif(ldif_path, dc_ip, remote_ldif)

    print(f'Importing via ldifde on {dc_ip} ...')
    ps = textwrap.dedent(f"""\
        $ErrorActionPreference = 'Stop'
        New-Item -ItemType Directory -Force -Path C:\\hive | Out-Null
        $result = & ldifde -i -f '{remote_ldif}' -k 2>&1
        Write-Output $result
        Write-Output "LDIFDE_EXIT:$LASTEXITCODE"
    """)

    t0 = time.time()
    session = _winrm_session(dc_ip)
    r = session.run_ps(ps)
    elapsed = time.time() - t0

    stdout = r.std_out.decode('utf-8', errors='replace')
    print(stdout[-2000:])
    print(f'\nImport completed in {elapsed:.1f}s')

    total = sum(COUNTS.values())
    rate = total / elapsed if elapsed > 0 else 0
    print(f'Throughput: ~{rate:.1f} obj/s  ({total:,} objects)')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'help'

    if mode == 'generate':
        generate()

    elif mode == 'import':
        if len(sys.argv) < 3:
            print('Usage: overload_tool.py import <dc_ip>')
            sys.exit(1)
        import_ldif(sys.argv[2])

    elif mode == 'run':
        if len(sys.argv) < 3:
            print('Usage: overload_tool.py run <dc_ip>')
            sys.exit(1)
        generate()
        import_ldif(sys.argv[2])

    else:
        print(__doc__)
        sys.exit(1)
