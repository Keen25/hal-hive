#!/usr/bin/env python3
"""Minimal WinRM runner for ar1 (NTLM). Usage: winrm_run.py '<powershell>'

Auth rejections are diagnosed, not blamed on the password — see hive_winrm.
"""
import sys

import hive_winrm

HOST = hive_winrm.DEFAULT_HOST
USER = hive_winrm.DEFAULT_USER

def run(ps):
    s = hive_winrm.session(HOST, USER)
    r = hive_winrm.run_ps(s, ps, HOST, USER)
    out = r.std_out.decode('utf-8', 'replace')
    err = r.std_err.decode('utf-8', 'replace')
    return r.status_code, out, err

if __name__ == '__main__':
    ps = sys.argv[1] if len(sys.argv) > 1 else '$PSVersionTable.PSVersion.ToString()'
    code, out, err = run(ps)
    if out: print(out, end='')
    if err: sys.stderr.write('[stderr]\n' + err)
    sys.exit(code)
