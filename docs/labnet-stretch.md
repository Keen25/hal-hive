# LABNet stretch — multi-host single-L2 fabric (VXLAN overlay)

How the HAL swarm runs workers on **more than one physical hypervisor host** while
keeping every worker on the **same `192.168.100.0/24` layer-2 segment (LABNet)**, so
they all reach the HAL control plane at a single address (`192.168.100.1`) with no
routing, NAT hair-pinning, or per-host IP ranges.

> **Currently deployed:** two **KVM/libvirt** hosts on Fedora (HAL + Lenin). The design
> is not KVM-specific — any hypervisor that can bridge an interface into a Linux VXLAN
> VTEP joins the same fabric (a Hyper-V peer is covered as a variant in step 2).

The mechanism is a **point-to-point unicast VXLAN overlay** (VNI 100) that bridges the
LABNet L2 domain across the physical LAN between hypervisors.

> This is a deployment addendum. The hive code itself is hypervisor- and
> topology-agnostic — workers only need to reach `HIVE_HAL_IP` (`:5001` queue,
> `:8418` git). Everything below is pure network plumbing under the workers.

---

## Topology (as currently deployed)

```
             physical LAN 192.168.1.0/24  (VXLAN underlay, UDP/4789)
   ┌──────────────────────────────┴───────────────────────────────┐
   │                                                                │
HAL host (KVM, Fedora)  .85  enp0s31f6         Lenin host (KVM, Fedora) .11  enp11s0
   │                                                                │
   │ virbr1 = LABNet 192.168.100.1/24                               │ labbr0 (plain bridge,
   │  ├─ vnet0..n → local KVM VMs                                    │  ├─ vnetN → local VMs
   │  │  (gateway + DHCP + DNS live here — authoritative IPAM)       │  │   no gateway/DHCP here
   │  └─ vxlan100 ─────────── VNI 100 / UDP 4789 ───────────────────┘  └─ vxlan100 (mirror)
   │      (enslaved to virbr1)                                          (enslaved to labbr0)
   │                                                                
   └── one stretched broadcast domain: 192.168.100.0/24 (LABNet) ──────────────────
```

- **HAL** (`192.168.1.85`, KVM/libvirt): owns LABNet — the libvirt NAT network `LABNet`
  (bridge `virbr1`, gateway/DHCP/DNS `192.168.100.1`). All local VMs attach to `virbr1`.
  HAL is the swarm control plane (`queue_server :5001`, `hive-git-http :8418`) and the
  **only** DHCP/gateway authority for `192.168.100.0/24`.
- **Lenin** (`192.168.1.11`, KVM/libvirt, Fedora laptop): runs a **plain bridge `labbr0`**
  (no IP, no DHCP, no NAT) carrying a **mirror `vxlan100`**. Its VMs attach directly to
  `labbr0` and get their LABNet lease from HAL over the overlay.
- **`vxlan100`**: VNI **100**, dstport **4789**. HAL side `local 192.168.1.85 remote
  192.168.1.11 dev enp0s31f6` enslaved to `virbr1`; Lenin side `local 192.168.1.11 remote
  192.168.1.85 dev enp11s0` enslaved to `labbr0`. Static unicast peer (no multicast).
- **MTU 1450** everywhere inside LABNet (50-byte VXLAN header on a 1500 underlay).

### Worker placement

| Worker | LABNet IP | Host | Path to HAL |
|--------|-----------|------|-------------|
| ar1, sql1, wg1, dc1, exchange, trust-dc | `192.168.100.x` | HAL (KVM) | local `virbr1` |
| Lenin-hosted VMs | `192.168.100.x` (HAL's pool) | Lenin (KVM) | `labbr0` → `vxlan100` → HAL |
| beast | `192.168.1.11` | Lenin host itself | physical LAN (not via LABNet) |

`beast` is the Lenin host's own worker and talks to HAL over the physical LAN directly;
it does not need the overlay. Only **VMs** on Lenin ride the stretched LABNet.

---

## Prerequisites

**Both hypervisor hosts**
- L3 reachability between the underlay IPs (`192.168.1.85 ⇄ 192.168.1.11`) and
  **UDP 4789 open** in both directions (VXLAN). No multicast needed (static unicast peer).
- Kernel VXLAN support (`ip link add type vxlan`; Linux `vxlan` module). Both current hosts
  are Fedora/KVM. A non-Linux hypervisor peer needs a VTEP it can bridge to (see step 2b).
- **Matching VNI (100) and dstport (4789)** on both VTEPs, each pointing `remote` at the other.
- **MTU headroom**: underlay ≥ 1500; every LABNet guest NIC clamped to **1450**
  (here dnsmasq pushes `dhcp-option=option:mtu,1450`). If guests use static IPs, set 1450 manually.
- One authority for LABNet IPAM — HAL's libvirt DHCP owns `192.168.100.0/24`; Lenin VMs
  either DHCP from HAL over the stretch or use static addresses outside the DHCP range.

**LABNet (HAL) — libvirt network `LABNet`**
- Bridge `virbr1`, `192.168.100.1/24`, `forward mode='nat'`, STP on.
- DHCP range `192.168.100.128–.254` with static host reservations by MAC for the fixed VMs.
- `dnsmasq:option dhcp-option=option:mtu,1450`.

---

## Steps

### 1. HAL / KVM side (authoritative — currently deployed)

LABNet is a standard libvirt NAT network (`virsh net-dumpxml LABNet`). The **stretch**
is added by a systemd oneshot that (re)creates `vxlan100` and enslaves it to `virbr1`
after the bridge exists. Unit `/etc/systemd/system/hal-vxlan-stretch.service`:

```ini
[Unit]
Description=HAL VXLAN L2 stretch to Lenin (vxlan100 -> virbr1/LABNet)
After=network-online.target libvirtd.service virtnetworkd.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
# wait for the LABNet bridge to exist (libvirt brings it up)
ExecStartPre=/bin/bash -c 'for i in $(seq 1 30); do /usr/bin/ip link show virbr1 >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
# idempotent recreate
ExecStartPre=-/usr/bin/ip link del vxlan100
ExecStart=/usr/bin/ip link add vxlan100 type vxlan id 100 local 192.168.1.85 remote 192.168.1.11 dstport 4789 dev enp0s31f6
ExecStart=/usr/bin/ip link set vxlan100 mtu 1450
ExecStart=/usr/bin/ip link set vxlan100 master virbr1
ExecStart=/usr/bin/ip link set vxlan100 up
ExecStop=-/usr/bin/ip link del vxlan100

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hal-vxlan-stretch.service
```

Notes:
- `local`/`dev` are **host-specific** — they must be *this* host's underlay IP and NIC.
  (The prior deployment used `local 192.168.1.84 dev enp0s20f0u5u1`, a USB NIC; it moved
  to onboard `enp0s31f6`/`.85`. If the underlay IP or NIC changes, edit both `local` and
  `dev`, then `systemctl restart hal-vxlan-stretch`.)
- The unit is `Type=oneshot`/`RemainAfterExit` and depends on `virtnetworkd`, so it runs
  once LABNet's `virbr1` is up and survives reboots.
- Ordering matters vs. the control plane: `hive-queue-server` binds `192.168.100.1:5001`
  once at startup and does **not** rebind — if it starts before `virbr1` is up it serves
  loopback only. Ensure the LABNet bridge (and ideally this stretch) are up before the
  queue server, or restart the queue server after the bridge appears.

### 2. Peer / Lenin side (verified — currently deployed)

The peer terminates the **same** VNI on its own **plain bridge `labbr0`** (no IP, no DHCP,
no NAT — HAL stays the sole IPAM authority) and attaches its VMs to that bridge.
Unit `/etc/systemd/system/hal-vxlan-stretch.service` on Lenin:

```ini
[Unit]
Description=HAL VXLAN L2 stretch to HAL (labbr0 + vxlan100 + labnet-stretch)
After=network-online.target libvirtd.service virtqemud.service virtnetworkd.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
# idempotent recreate
ExecStartPre=-/usr/bin/virsh net-destroy labnet-stretch
ExecStartPre=-/usr/bin/ip link del vxlan100
ExecStartPre=-/usr/bin/ip link del labbr0
# build the local L2 bridge (no IP: HAL owns the gateway/DHCP)
ExecStart=/usr/bin/ip link add name labbr0 type bridge
ExecStart=/usr/bin/ip link set labbr0 mtu 1450
ExecStart=/usr/bin/ip link set labbr0 type bridge stp_state 0
ExecStart=/usr/bin/ip link set labbr0 up
# VXLAN back to HAL, enslaved to labbr0
ExecStart=/usr/bin/ip link add vxlan100 type vxlan id 100 local 192.168.1.11 remote 192.168.1.85 dstport 4789 dev enp11s0
ExecStart=/usr/bin/ip link set vxlan100 mtu 1450
ExecStart=/usr/bin/ip link set vxlan100 master labbr0
ExecStart=/usr/bin/ip link set vxlan100 up
ExecStart=-/usr/bin/virsh net-start labnet-stretch
ExecStop=-/usr/bin/virsh net-destroy labnet-stretch
ExecStop=-/usr/bin/ip link del vxlan100
ExecStop=-/usr/bin/ip link del labbr0

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hal-vxlan-stretch.service
```

Notes:
- `local`/`dev` are this host's underlay (`192.168.1.11` / `enp11s0`); `remote` is HAL (`.85`).
- Lenin builds its **own** bridge `labbr0` rather than reusing a libvirt NAT network, so it
  contributes no gateway/DHCP — VMs lease from HAL across the overlay. Attach each Lenin VM
  directly to `labbr0` (`<interface type='bridge'><source bridge='labbr0'/>`).
- The `virsh net-start labnet-stretch` line is optional/legacy (that libvirt network is not
  defined here, so the `-`-prefixed call is a harmless no-op); VMs use the bridge directly.

### 2b. Peer variant — Windows Hyper-V (not currently deployed)

Hyper-V has no native VXLAN VTEP. To join a Hyper-V host to the same fabric, terminate the
overlay on a VTEP Hyper-V can bridge to — e.g. Open vSwitch for Hyper-V, or a small Linux
"router" VM that holds `vxlan100` (VNI 100 / UDP 4789 / `remote 192.168.1.85` / MTU 1450)
and a second NIC on the external vSwitch. Attach LABNet VMs to that external vSwitch.

### 3. Join a Lenin-hosted VM to the swarm

Same as any worker — the overlay is transparent:
1. Give the VM a LABNet address (`192.168.100.x`; DHCP from HAL over the stretch, or static + MTU 1450).
2. Bootstrap the worker with `HIVE_HAL_IP=192.168.100.1` (queue `:5001`, git `:8418`).
3. Confirm it appears in `halctl workers`.

---

## Verification

```bash
# HAL: overlay up and enslaved to LABNet
ip -d link show vxlan100          # expect: vxlan id 100 remote 192.168.1.11 local 192.168.1.85 dstport 4789, master virbr1
ip link show master virbr1        # expect vxlan100 + vnetN listed
bridge fdb show dev vxlan100      # expect: 00:00:00:00:00:00 dst 192.168.1.11 (the peer VTEP)
                                  # once a Lenin VM sends traffic, its MAC appears here: <mac> dst 192.168.1.11

# Lenin: mirror overlay up and enslaved to labbr0
ip -d link show vxlan100          # expect: vxlan id 100 remote 192.168.1.85 local 192.168.1.11 dstport 4789, master labbr0
ip link show master labbr0        # expect vxlan100 (+ vnetN when VMs run)

# From a Lenin-hosted VM
ping 192.168.100.1                # LABNet gateway (HAL) reachable over the stretch
curl -s http://192.168.100.1:5001/version   # control plane reachable

# Control plane sees the worker
python3 halctl.py workers
```

### Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| Lenin VMs can't reach `192.168.100.1` | peer `vxlan100` down / VNI or dstport mismatch / UDP 4789 blocked on underlay |
| Ping works but TCP (git clone / large payloads) stalls | MTU not clamped to 1450 on a guest — set guest NIC MTU / verify DHCP option |
| Workers show offline after HAL reboot | queue server bound before `virbr1`/stretch came up — restart `hive-queue-server` |
| `bridge fdb` missing the peer entry | wrong `remote`/`dev` in the unit, or underlay IP changed — edit `local`/`remote`/`dev` and restart the unit |
