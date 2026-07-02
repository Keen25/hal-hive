#!/usr/bin/env python3
"""Minimal WinRM runner for ar1 (NTLM). Usage: winrm_run.py '<powershell>'"""
import sys, os, winrm

HOST = '192.168.100.11'
USER = r'fodor\a'
PASS = os.environ.get('LAB_PASS')
if not PASS:
    raise SystemExit('LAB_PASS not set — source ~/.hive/hive.env')

def run(ps):
    s = winrm.Session(f'http://{HOST}:5985/wsman',
                      auth=(USER, PASS), transport='ntlm')
    r = s.run_ps(ps)
    out = r.std_out.decode('utf-8', 'replace')
    err = r.std_err.decode('utf-8', 'replace')
    return r.status_code, out, err

if __name__ == '__main__':
    ps = sys.argv[1] if len(sys.argv) > 1 else '$PSVersionTable.PSVersion.ToString()'
    code, out, err = run(ps)
    if out: print(out, end='')
    if err: sys.stderr.write('[stderr]\n' + err)
    sys.exit(code)
