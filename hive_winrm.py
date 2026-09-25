#!/usr/bin/env python3
"""Shared WinRM session factory for the lab boxes, with an honest failure mode.

Why this exists: pywinrm collapses every authentication outcome into
``InvalidCredentialsError("the specified credentials were rejected by the
server")``. That message is actively misleading. A broken machine-account secure
channel on the target (NTLM logon fails with STATUS_TRUSTED_RELATIONSHIP_FAILURE,
0xC000018D) produces the exact same exception as a genuinely wrong password —
which is how a WinRM outage on ar1 was misdiagnosed as "credentials rejected"
while the password was in fact valid against the DC.

On failure we therefore probe what the endpoint actually advertises and print
the causes worth checking, in likelihood order, instead of blaming the password.
"""
from __future__ import annotations

import os
import sys

import requests
import winrm
from winrm.exceptions import InvalidCredentialsError

DEFAULT_HOST = '192.168.100.11'          # ar1
DEFAULT_USER = r'fodor\a'


def _advertised_auth(host: str, timeout: int = 8) -> str:
    """WWW-Authenticate schemes the endpoint offers, or '' if unreachable."""
    try:
        r = requests.post(f'http://{host}:5985/wsman', data='', timeout=timeout,
                          headers={'Content-Type':
                                   'application/soap+xml;charset=UTF-8'})
        return r.headers.get('WWW-Authenticate', '')
    except requests.RequestException as e:
        return f'<unreachable: {e.__class__.__name__}>'


def auth_failure_hint(host: str, user: str) -> str:
    """Multi-line operator hint for a rejected WinRM logon."""
    return '\n'.join([
        f'WinRM rejected the logon for {user} at {host}.',
        f'  endpoint offers: {_advertised_auth(host) or "<no challenge>"}',
        '',
        'pywinrm reports every rejection as "invalid credentials". Check, in order:',
        '  1. Target secure channel. A broken machine account fails EVERY domain',
        '     logon with 0xC000018D regardless of password. Confirm on the target:',
        '       pwsh ~/hive/ar1exec.ps1 -Ip <ip> -Command "Test-ComputerSecureChannel"',
        '     Repair (needs domain creds):  Test-ComputerSecureChannel -Repair',
        '  2. The password itself. Verify against the DC WITHOUT NTLM - an LDAPS',
        '     simple bind as user@fodor.monster.local on port 636 either binds or',
        '     returns an AD sub-code (52e bad password, 775 locked, 533 disabled).',
        '  3. Account state: lockoutTime, userAccountControl, Protected Users',
        '     membership (Protected Users cannot use NTLM at all).',
        '',
        'ar1exec.ps1 uses the box LOCAL Administrator and keeps working through',
        'all of the above - it is the out-of-band channel when the domain is down.',
    ])


def session(host: str = DEFAULT_HOST, user: str = DEFAULT_USER,
            password: str | None = None) -> winrm.Session:
    """Build an NTLM WinRM session. Exits with a real diagnosis, not a guess."""
    password = password or os.environ.get('LAB_PASS')
    if not password:
        raise SystemExit('LAB_PASS not set — source ~/.hive/hive.env')
    return winrm.Session(f'http://{host}:5985/wsman', auth=(user, password),
                         transport='ntlm')


def run_ps(sess: winrm.Session, ps: str, host: str = DEFAULT_HOST,
           user: str = DEFAULT_USER):
    """run_ps that turns an auth rejection into an actionable message."""
    try:
        return sess.run_ps(ps)
    except InvalidCredentialsError:
        sys.stderr.write(auth_failure_hint(host, user) + '\n')
        raise SystemExit(126)
