#!/usr/bin/env python3
"""Push a local file to ar1 over WinRM (NTLM) via chunked base64.

Usage: winrm_push.py <local_path> <remote_path>
"""
import base64, sys, os, winrm

HOST = '192.168.100.11'
USER = r'fodor\a'
PASS = os.environ.get('LAB_PASS')
if not PASS:
    raise SystemExit('LAB_PASS not set — source ~/.hive/hive.env')
CHUNK = 2000  # base64 chars/chunk; run_ps UTF16+base64 inflates ~2.7x -> stay < 8k cmd limit


def main(local, remote):
    data = open(local, 'rb').read()
    b64 = base64.b64encode(data).decode('ascii')
    s = winrm.Session(f'http://{HOST}:5985/wsman', auth=(USER, PASS),
                      transport='ntlm')

    # ensure parent dir + start clean
    parent = remote.rsplit('\\', 1)[0]
    s.run_ps(f'New-Item -ItemType Directory -Force -Path "{parent}" | Out-Null; '
             f'if (Test-Path "{remote}.b64") {{ Remove-Item "{remote}.b64" }}')

    for i in range(0, len(b64), CHUNK):
        part = b64[i:i + CHUNK]
        r = s.run_ps(f'Add-Content -Path "{remote}.b64" -Value "{part}" -NoNewline')
        if r.status_code != 0:
            sys.exit(f'chunk {i} failed: {r.std_err.decode()[:200]}')

    # decode base64 file -> binary, then verify size
    r = s.run_ps(
        f'$b=[IO.File]::ReadAllText("{remote}.b64"); '
        f'[IO.File]::WriteAllBytes("{remote}",[Convert]::FromBase64String($b)); '
        f'Remove-Item "{remote}.b64"; (Get-Item "{remote}").Length')
    out = r.std_out.decode().strip()
    if r.status_code != 0:
        sys.exit(f'decode failed: {r.std_err.decode()[:300]}')
    print(f'{remote}: {out} bytes (local {len(data)})')
    if out != str(len(data)):
        sys.exit('SIZE MISMATCH')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
