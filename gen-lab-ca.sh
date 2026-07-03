#!/usr/bin/env bash
# gen-lab-ca.sh — mini lab CA + hive server cert for the HAL hive (issue #8 / S5).
#
# Terminates TLS on queue_server.py (:5001) and git_http_server.py (:8418) so the
# control/RO/GH tokens and git packs no longer transit in clear text.
#
# What it produces (in $HIVE_CA_DIR, default ~/.hive-ca, mode 700):
#   ca.key      long-lived root CA private key  (NEVER leaves this host)
#   ca.crt      root CA cert  — install this into each client's trust store
#   server.key  hive server private key
#   server.crt  hive server cert, signed by ca.crt, with IP+DNS SANs so workers
#               keep connecting by 192.168.100.1 etc. with no cert-name errors
#
# Idempotent: the CA is generated once and reused; the server cert is re-issued
# on every run (cheap, lets you add SANs). Pass --force-ca to rotate the root.
#
# Usage:
#   ./gen-lab-ca.sh [extra-SAN ...]      # extra SANs: IPs or DNS names
#   HIVE_CA_DIR=/path ./gen-lab-ca.sh
#   ./gen-lab-ca.sh --force-ca           # regenerate the root CA (re-distribute!)
#
# After running, point the servers at the cert (default HTTP unless both set):
#   export HIVE_TLS_CERT=~/.hive-ca/server.crt HIVE_TLS_KEY=~/.hive-ca/server.key
set -euo pipefail

CA_DIR="${HIVE_CA_DIR:-$HOME/.hive-ca}"
CA_DAYS="${HIVE_CA_DAYS:-3650}"      # root CA lifetime (10y)
SRV_DAYS="${HIVE_SRV_DAYS:-825}"     # server cert lifetime (<=825 keeps clients happy)
LAB_IP="${HIVE_LAB_IP:-192.168.100.1}"

FORCE_CA=0
EXTRA_SANS=()
for a in "$@"; do
  case "$a" in
    --force-ca) FORCE_CA=1 ;;
    *) EXTRA_SANS+=("$a") ;;
  esac
done

umask 077
mkdir -p "$CA_DIR"
cd "$CA_DIR"

# --- root CA (self-signed) --------------------------------------------------
if [[ ! -f ca.key || ! -f ca.crt || "$FORCE_CA" == "1" ]]; then
  echo "[ca] generating root CA (RSA 4096, ${CA_DAYS}d) in $CA_DIR"
  openssl genrsa -out ca.key 4096 2>/dev/null
  openssl req -x509 -new -nodes -key ca.key -sha256 -days "$CA_DAYS" \
    -subj "/CN=HAL Hive Lab CA/O=HAL Hive" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out ca.crt 2>/dev/null
else
  echo "[ca] reusing existing root CA (pass --force-ca to rotate)"
fi
chmod 600 ca.key

# --- SAN list ---------------------------------------------------------------
# Always cover loopback + the lab NIC; auto-add every non-loopback IPv4 on this
# host (so LAN access works too) and the hostname; then any user-supplied SANs.
declare -a IPS DNS
IPS=("127.0.0.1" "$LAB_IP")
DNS=("$(hostname -s)" "localhost")
if command -v ip >/dev/null 2>&1; then
  while read -r a; do IPS+=("$a"); done < <(
    ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
fi
for s in "${EXTRA_SANS[@]:-}"; do
  [[ -z "$s" ]] && continue
  if [[ "$s" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then IPS+=("$s"); else DNS+=("$s"); fi
done

san=""; i=1
declare -A seen
for d in "${DNS[@]}"; do
  [[ -z "$d" || -n "${seen[dns:$d]:-}" ]] && continue
  seen[dns:$d]=1; san+="DNS.$i = $d"$'\n'; i=$((i+1))
done
i=1
for p in "${IPS[@]}"; do
  [[ -z "$p" || -n "${seen[ip:$p]:-}" ]] && continue
  seen[ip:$p]=1; san+="IP.$i = $p"$'\n'; i=$((i+1))
done

cat > server.ext <<EOF
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt
[alt]
$san
EOF

# --- server cert (signed by the CA) -----------------------------------------
echo "[srv] issuing server cert (${SRV_DAYS}d) with SANs:"
sed -n '/\[alt\]/,$p' server.ext | grep -E '^(DNS|IP)\.' | sed 's/^/       /'
openssl genrsa -out server.key 2048 2>/dev/null
chmod 600 server.key
openssl req -new -key server.key -subj "/CN=$(hostname -s)/O=HAL Hive" -out server.csr 2>/dev/null
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days "$SRV_DAYS" -sha256 -extfile server.ext -out server.crt 2>/dev/null
rm -f server.csr

echo
echo "[ok] wrote $CA_DIR/{ca.crt,ca.key,server.crt,server.key}"
echo
echo "Enable TLS on the hive servers:"
echo "  export HIVE_TLS_CERT=$CA_DIR/server.crt"
echo "  export HIVE_TLS_KEY=$CA_DIR/server.key"
echo
echo "Trust the CA on this host (Fedora/RHEL):"
echo "  sudo cp $CA_DIR/ca.crt /etc/pki/ca-trust/source/anchors/hal-hive-ca.crt && sudo update-ca-trust"
echo "On a Windows worker (e.g. ar1), import $CA_DIR/ca.crt then:"
echo "  certutil -addstore -f Root ca.crt"
