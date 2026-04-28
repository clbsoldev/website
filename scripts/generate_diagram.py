#!/usr/bin/env python3
"""
generate_diagram.py
====================
Fetches devices and VMs from Netbox Cloud Free API and generates an SVG
network diagram with three visual rows:

  Row 1 (top):    [ SaaS Services ]  |  [ Public Infrastructure ]
  Row 2 (middle): [         ☁ INTERNET          ]
  Row 3 (bottom): [           Home Lab           ]

────────────────────────────────────────────────────────────────────────────
SAAS SERVICES  (static, configured here – not from Netbox)
────────────────────────────────────────────────────────────────────────────
Edit SAAS_NODES below to add/remove SaaS entries. Each entry:
    {
        "name":        "Webex Cloud",           # bold headline
        "description": "Hybrid Services · Calling",  # dim subtitle
        "color":       "#0077a8",               # optional border accent
    }

These are intentionally static – SaaS cannot be modelled as Netbox devices.

────────────────────────────────────────────────────────────────────────────
FILTERING  –  pure tag-based, no heuristics
────────────────────────────────────────────────────────────────────────────
  Tag slug            Zone
  ─────────────────────────────────────────
  diagram-public      Public Infrastructure  (top-right, orange)
  diagram-homelab     Home Lab               (bottom, green)
  diagram-exclude     Always skip
  diagram-no-vms      Don't render VM children for this device

────────────────────────────────────────────────────────────────────────────
PROXMOX CLUSTER HIERARCHY
────────────────────────────────────────────────────────────────────────────
Lab devices that belong to a Netbox virtualisation cluster get their VMs
auto-fetched and rendered as child cards. Tag a device with diagram-no-vms
to suppress this (use on VPS hosts).

────────────────────────────────────────────────────────────────────────────
CONNECTIONS
────────────────────────────────────────────────────────────────────────────
Physical cables (/dcim/cables/): solid lines.
Logical tunnels declared in LOGICAL_TUNNELS: dashed lines with label.

────────────────────────────────────────────────────────────────────────────
ENV VARS  (GitHub Actions Secrets)
────────────────────────────────────────────────────────────────────────────
  NETBOX_URL    –  https://yourinstance.netboxcloud.com
  NETBOX_TOKEN  –  read-only API token

OUTPUTS:  assets/diagram.svg  ·  assets/diagram.png  ·  assets/diagram_meta.json
"""

import os, sys, json, datetime, textwrap, urllib.request, urllib.error
from collections import defaultdict

# ══════════════════════════════════════════════════════════════════════════════
#  ★  CONFIGURE HERE
# ══════════════════════════════════════════════════════════════════════════════

# SaaS services displayed in the top-left zone.
# Not fetched from Netbox – edit this list manually.
SAAS_NODES: list[dict] = [
    {
        "name":        "Webex Cloud",
        "description": "Hybrid Services · Calling · Messaging",
        "color":       "#0077a8",
    },
    {
        "name":        "Netbox Cloud",
        "description": "IPAM / DCIM · Free Tier · diagram source",
        "color":       "#9b59b6",
    },
    {
        "name":        "Microsoft 365",
        "description": "Exchange · Teams · Hybrid Calendar",
        "color":       "#0078d4",
    },
    # Add more SaaS entries here, e.g.:
    # {
    #     "name":        "cron-job.org",
    #     "description": "Nagios heartbeat scheduler",
    #     "color":       "#ff8c42",
    # },
]

# Logical/overlay tunnels – FALLBACK ONLY.
# At runtime these are fetched from Netbox /vpn/tunnels/ automatically.
# Only used when Netbox returns no tunnels or is unreachable.
# Format: (device_name_A, device_name_B, label, style)
LOGICAL_TUNNELS_FALLBACK: list[tuple[str, str, str, str]] = [
    ("VPS nginx", "Unifi Gateway", "WireGuard VPN", "wireguard"),
]

# ══════════════════════════════════════════════════════════════════════════════
#  Config / tags
# ══════════════════════════════════════════════════════════════════════════════

NETBOX_URL   = os.environ.get("NETBOX_URL", "").rstrip("/")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN", "")

OUTPUT_SVG  = "assets/diagram.svg"
OUTPUT_PNG  = "assets/diagram.png"
OUTPUT_META = "assets/diagram_meta.json"

TAG_PUBLIC  = "diagram-public"
TAG_HOMELAB = "diagram-homelab"
TAG_EXCLUDE = "diagram-exclude"
TAG_NO_VMS  = "diagram-no-vms"

# ── Static fallback (when Netbox is unreachable / no tagged devices) ──────────
FALLBACK_PUBLIC: list[dict] = [
    {"name": "VPS · Monitoring",  "description": "Nagios Core · cron-job.org heartbeat", "status": "active", "vms": [], "cluster_id": None, "no_vms": True},
    {"name": "VPS nginx",          "description": "Reverse Proxy · WireGuard endpoint",   "status": "active", "vms": [], "cluster_id": None, "no_vms": True},
]
FALLBACK_HOMELAB: list[dict] = [
    {"name": "Unifi Gateway",   "description": "VLAN segmentation · WireGuard endpoint", "status": "active", "vms": [], "cluster_id": None, "no_vms": True},
    {"name": "Proxmox Cluster", "description": "VMs + LXC · lab virtualisation base",   "status": "active",
     "vms": [
         {"name": "example-vm-1", "description": "tag in Netbox to auto-discover"},
         {"name": "example-vm-2", "description": "auto-discovered via cluster"},
     ], "cluster_id": 1, "no_vms": False},
    {"name": "Ansible · RasPi",  "description": "Control node · config management",     "status": "active", "vms": [], "cluster_id": None, "no_vms": True},
    {"name": "Synology NAS",     "description": "Docker Stacks · n8n · phpIPAM",        "status": "active", "vms": [], "cluster_id": None, "no_vms": True},
    {"name": "Cisco Switch",     "description": "VLAN switch · IP Phones · SIP/Calling","status": "active", "vms": [], "cluster_id": None, "no_vms": True},
    {"name": "2N Intercom",      "description": "Door comm. · Access Manager",           "status": "active", "vms": [], "cluster_id": None, "no_vms": True},
]

# ══════════════════════════════════════════════════════════════════════════════
#  Netbox API
# ══════════════════════════════════════════════════════════════════════════════

def nb_get(path: str) -> list:
    if not NETBOX_URL or not NETBOX_TOKEN:
        return []
    url: str | None = f"{NETBOX_URL}/api{path}?limit=200"
    results: list = []
    while url:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Token {NETBOX_TOKEN}", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
                results.extend(data.get("results", []))
                url = data.get("next")
        except urllib.error.URLError as exc:
            print(f"[WARN] Netbox API error ({path}): {exc}", file=sys.stderr)
            break
    return results

def _tags(obj: dict) -> set[str]:
    return {t.get("slug", "") for t in (obj.get("tags") or [])}

def _str(obj: dict | None, key: str) -> str:
    return ((obj or {}).get(key) or "").strip()

# ══════════════════════════════════════════════════════════════════════════════
#  Fetch & classify
# ══════════════════════════════════════════════════════════════════════════════

def fetch_all() -> tuple[list[dict], list[dict], list[tuple[str,str]], list[tuple[str,str,str,str]], int]:
    """
    Returns:
        pub_nodes, lab_nodes, cables, tunnels, raw_device_count
    """
    print("[INFO] Fetching /dcim/devices/ …")
    raw_devices = nb_get("/dcim/devices/")

    pub_nodes: list[dict] = []
    lab_nodes: list[dict] = []

    for d in raw_devices:
        tags = _tags(d)
        if TAG_EXCLUDE in tags:
            continue
        in_pub = TAG_PUBLIC  in tags
        in_lab = TAG_HOMELAB in tags
        if not in_pub and not in_lab:
            continue
        node = {
            "name":        d.get("name") or "unnamed",
            "description": d.get("description") or _str(d.get("device_role"), "name"),
            "status":      _str(d.get("status"), "value") or "active",
            "cluster_id":  (d.get("cluster") or {}).get("id"),
            "no_vms":      TAG_NO_VMS in tags,
            "vms":         [],
        }
        if in_pub:
            pub_nodes.append(node)
        if in_lab:
            lab_nodes.append(node)

    print(f"[INFO] Tagged → public={len(pub_nodes)}, homelab={len(lab_nodes)} / {len(raw_devices)} total")

    # VMs for homelab cluster members
    if lab_nodes:
        print("[INFO] Fetching /virtualization/virtual-machines/ …")
        raw_vms = nb_get("/virtualization/virtual-machines/")
        cluster_vms: dict[int, list[dict]] = defaultdict(list)
        for vm in raw_vms:
            cid = (vm.get("cluster") or {}).get("id")
            if cid:
                cluster_vms[cid].append({
                    "name":        vm.get("name") or "unnamed-vm",
                    "description": vm.get("description") or _str(vm.get("role"), "name"),
                    "status":      _str(vm.get("status"), "value") or "active",
                })
        for node in lab_nodes:
            if not node["no_vms"] and node["cluster_id"]:
                node["vms"] = cluster_vms.get(node["cluster_id"], [])
                if node["vms"]:
                    print(f"  → {node['name']}: {len(node['vms'])} VMs")

    # Physical cables
    print("[INFO] Fetching /dcim/cables/ …")
    raw_cables = nb_get("/dcim/cables/")
    all_names = {n["name"] for n in pub_nodes} | {n["name"] for n in lab_nodes}
    cables: list[tuple[str, str]] = []
    for cable in raw_cables:
        def _dev(terms: list) -> str | None:
            for t in (terms or []):
                dev = (t.get("object") or {}).get("device") or t.get("object") or {}
                n = dev.get("name")
                if n and n in all_names:
                    return n
            return None
        na, nb_ = _dev(cable.get("a_terminations") or []), _dev(cable.get("b_terminations") or [])
        if na and nb_ and na != nb_:
            cables.append((na, nb_))

    # ── VPN Tunnels from Netbox (/vpn/tunnels/) ────────────────────────────────
    # Netbox stores each tunnel with terminations that reference interfaces,
    # which in turn reference devices. We resolve device names from there.
    # tunnel.encapsulation slug "wireguard" → style "wireguard", else "logical".
    print("[INFO] Fetching /vpn/tunnels/ …")
    raw_tunnels = nb_get("/vpn/tunnels/")
    tunnels: list[tuple[str, str, str, str]] = []

    if raw_tunnels:
        print(f"[INFO] Found {len(raw_tunnels)} VPN tunnel(s) in Netbox")
        # We need terminations per tunnel: /vpn/tunnel-terminations/?tunnel_id=X
        for t in raw_tunnels:
            tid      = t.get("id")
            label    = t.get("name") or "VPN Tunnel"
            encap    = _str(t.get("encapsulation"), "value").lower()
            style    = "wireguard" if "wireguard" in encap else "logical"

            terms = nb_get(f"/vpn/tunnel-terminations/?tunnel_id={tid}") if tid else []
            # Extract device names from termination → interface → device
            dev_names: list[str] = []
            for term in terms:
                iface = term.get("termination") or {}
                dev   = iface.get("device") or {}
                name  = dev.get("name") or ""
                if name and name not in dev_names:
                    dev_names.append(name)

            if len(dev_names) >= 2:
                tunnels.append((dev_names[0], dev_names[1], label, style))
                print(f"  → Tunnel '{label}' ({style}): {dev_names[0]} ↔ {dev_names[1]}")
            elif len(dev_names) == 1:
                print(f"  [WARN] Tunnel '{label}' only has 1 resolved endpoint: {dev_names[0]}")
            else:
                print(f"  [WARN] Tunnel '{label}' has no resolvable device endpoints – skipped")
    else:
        print("[INFO] No VPN tunnels in Netbox – using fallback list")

    if not tunnels:
        tunnels = LOGICAL_TUNNELS_FALLBACK

    return pub_nodes, lab_nodes, cables, tunnels, len(raw_devices)

# ══════════════════════════════════════════════════════════════════════════════
#  SVG layout helpers
# ══════════════════════════════════════════════════════════════════════════════

C = {
    "bg":     "#0a0e14", "surface": "#111620", "border": "#1e2a3a",
    "pub":    "#ff8c42", "lab":     "#00e5a0",  "saas":  "#1e2a3a",
    "acc":    "#00d4ff", "dim":     "#5a7a94",  "head":  "#e8f4ff",
    "off":    "#3a4a5a", "cable":   "#2a3a4a",
    "vm_bdr": "#2a3a4a", "vm_bg":   "#0d1219",
}

DW, DH       = 158, 70    # device / SaaS card
HGAP, VGAP  = 14,  10
VM_W, VM_H  = 126, 40
VM_HGAP     = 8
INTERNET_H  = 70           # height of the Internet row
ZONE_PAD    = 28           # outer padding
ZONE_HDR    = 20           # zone header text height

def _wrap(t: str, w: int = 18) -> list[str]:
    return textwrap.wrap(t, w) or [t]

def _desc_lines(t: str, w: int = 24) -> list[str]:
    return textwrap.wrap(t, w)[:2] if t else []

def _row_xs(count: int, zone_x: int, zone_w: int, cw: int, gap: int) -> list[int]:
    total = count * cw + max(0, count - 1) * gap
    x0 = zone_x + max(0, (zone_w - total) // 2)
    return [x0 + i * (cw + gap) for i in range(count)]

def _content_h(nodes: list[dict], zone_w: int) -> int:
    """Total height of card rows + VM children."""
    if not nodes:
        return DH
    max_row = max(1, (zone_w + HGAP) // (DW + HGAP))
    total = 0
    i = 0
    while i < len(nodes):
        row = nodes[i : i + max_row]
        has_vms = any(n.get("vms") for n in row)
        total += DH + (VM_H + 28 if has_vms else 0) + VGAP * 2
        i += max_row
    return total

def _zone_h(nodes: list[dict], zone_w: int) -> int:
    return _content_h(nodes, zone_w) + ZONE_HDR + 20

# ══════════════════════════════════════════════════════════════════════════════
#  SVG builder
# ══════════════════════════════════════════════════════════════════════════════

def build_svg(
    saas:    list[dict],
    pub:     list[dict],
    lab:     list[dict],
    cables:  list[tuple[str, str]],
    tunnels: list[tuple[str, str, str, str]],
) -> str:
    W = 960

    # ── Column widths for top row (SaaS left | Public right) ─────────────────
    # SaaS gets ~38% of width, Public gets ~62%
    TOP_PAD   = ZONE_PAD
    TOP_GAP   = 16
    SAAS_W    = int((W - 2 * TOP_PAD - TOP_GAP) * 0.38)
    PUB_W     = W - 2 * TOP_PAD - TOP_GAP - SAAS_W

    saas_x    = TOP_PAD
    pub_x     = TOP_PAD + SAAS_W + TOP_GAP
    top_y     = ZONE_PAD

    saas_h    = _zone_h(saas, SAAS_W) if saas else DH + ZONE_HDR + 20
    pub_h     = _zone_h(pub,  PUB_W)  if pub  else DH + ZONE_HDR + 20
    top_h     = max(saas_h, pub_h)

    # ── Internet row ──────────────────────────────────────────────────────────
    inet_y    = top_y + top_h + 20

    # ── Home Lab row ──────────────────────────────────────────────────────────
    LAB_W     = W - 2 * ZONE_PAD
    lab_y     = inet_y + INTERNET_H + 20
    lab_h     = _zone_h(lab, LAB_W) if lab else DH + ZONE_HDR + 20

    H = lab_y + lab_h + ZONE_PAD + 8

    svg: list[str] = []
    a = svg.append
    conn_lines: list[str] = []   # drawn after zones
    pos_index: dict[str, tuple[int, int]] = {}

    a(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
      f'font-family="IBM Plex Mono, monospace">')
    a(f'<rect width="{W}" height="{H}" fill="{C["bg"]}"/>')

    # ── Render helpers ────────────────────────────────────────────────────────

    def render_card(
        x: int, y: int,
        name: str, description: str,
        bdr_color: str,
        card_w: int = DW, card_h: int = DH,
    ) -> tuple[int, int]:
        """Draw a card, return center (cx, cy)."""
        ccx, ccy = x + card_w // 2, y + card_h // 2
        a(f'<rect x="{x}" y="{y}" width="{card_w}" height="{card_h}" rx="2" '
          f'fill="{C["bg"]}" stroke="{bdr_color}" stroke-width="1"/>')

        desc_lines  = _desc_lines(description, 26)
        name_lines  = _wrap(name, 18)[:2]
        name_h      = len(name_lines) * 13
        total_h     = name_h + (len(desc_lines) * 11 + 4 if desc_lines else 0)
        start_y     = y + (card_h - total_h) // 2 + 12

        for j, ln in enumerate(name_lines):
            a(f'<text x="{ccx}" y="{start_y + j*13}" text-anchor="middle" '
              f'fill="{C["head"]}" font-size="9" font-weight="600">{ln}</text>')
        desc_y = start_y + name_h + 4
        for j, dl in enumerate(desc_lines):
            a(f'<text x="{ccx}" y="{desc_y + j*11}" text-anchor="middle" '
              f'fill="{C["dim"]}" font-size="7">{dl}</text>')
        return ccx, ccy

    def render_vm(x: int, y: int, vm: dict) -> None:
        ccx = x + VM_W // 2
        a(f'<rect x="{x}" y="{y}" width="{VM_W}" height="{VM_H}" rx="2" '
          f'fill="{C["vm_bg"]}" stroke="{C["vm_bdr"]}" stroke-width="1" stroke-dasharray="2,2"/>')
        n_lines = _wrap(vm["name"], 16)[:2]
        for j, ln in enumerate(n_lines):
            a(f'<text x="{ccx}" y="{y+14+j*12}" text-anchor="middle" '
              f'fill="{C["head"]}" font-size="8" font-weight="600">{ln}</text>')
        if desc := vm.get("description", ""):
            dl = textwrap.wrap(desc, 20)[:1]
            if dl:
                a(f'<text x="{ccx}" y="{y+VM_H-8}" text-anchor="middle" '
                  f'fill="{C["dim"]}" font-size="6">{dl[0]}</text>')

    def render_device_rows(
        nodes: list[dict],
        zone_x: int, zone_w: int,
        card_y_base: int,
        zone_color: str,
    ) -> None:
        """Lay out device cards in rows, with optional VM children."""
        max_row = max(1, (zone_w + HGAP) // (DW + HGAP))
        dy = 0
        i = 0
        while i < len(nodes):
            row = nodes[i : i + max_row]
            xs  = _row_xs(len(row), zone_x, zone_w, DW, HGAP)
            has_vms = any(n.get("vms") for n in row)

            for j, node in enumerate(row):
                cx, cy = render_card(
                    xs[j], card_y_base + dy,
                    node["name"], node.get("description", ""),
                    zone_color if node.get("status", "active") == "active" else C["off"],
                )
                pos_index[node["name"]] = (cx, cy)

                if node.get("vms"):
                    vm_y = card_y_base + dy + DH + 20
                    vm_xs = _row_xs(len(node["vms"]), zone_x, zone_w, VM_W, VM_HGAP)
                    for k, vm in enumerate(node["vms"]):
                        vcx = vm_xs[k] + VM_W // 2
                        conn_lines.append(
                            f'<line x1="{cx}" y1="{card_y_base + dy + DH}" '
                            f'x2="{vcx}" y2="{vm_y}" '
                            f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="2,2"/>'
                        )
                        render_vm(vm_xs[k], vm_y, vm)

            dy += DH + (VM_H + 28 if has_vms else 0) + VGAP * 2
            i  += max_row

    def render_zone_box(x: int, y: int, w: int, h: int, color: str, label: str) -> None:
        a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" '
          f'fill="none" stroke="{color}" stroke-width="1" stroke-dasharray="4,4"/>')
        a(f'<text x="{x+10}" y="{y+15}" fill="{color}" font-size="8" letter-spacing="2">{label}</text>')

    # ── TOP ROW: SaaS (left) + Public (right) ─────────────────────────────────

    # SaaS zone
    render_zone_box(saas_x, top_y, SAAS_W, top_h, C["saas"], "⬡ SAAS SERVICES")
    saas_xs = _row_xs(len(saas), saas_x, SAAS_W, DW, HGAP)
    saas_base_y = top_y + ZONE_HDR + 8
    row_max = max(1, (SAAS_W + HGAP) // (DW + HGAP))
    dy = 0
    for idx, node in enumerate(saas):
        row_idx = idx // row_max
        col_idx = idx % row_max
        row_nodes_count = min(row_max, len(saas) - row_idx * row_max)
        xs_row = _row_xs(row_nodes_count, saas_x, SAAS_W, DW, HGAP)
        x_pos = xs_row[col_idx]
        y_pos = saas_base_y + row_idx * (DH + VGAP * 2)
        color = node.get("color", C["acc"])
        ccx, ccy = render_card(x_pos, y_pos, node["name"], node.get("description", ""), color)
        pos_index[node["name"]] = (ccx, ccy)

    # Public zone
    render_zone_box(pub_x, top_y, PUB_W, top_h, C["pub"], "☁ PUBLIC INFRASTRUCTURE")
    if pub:
        render_device_rows(pub, pub_x, PUB_W, top_y + ZONE_HDR + 8, C["pub"])

    # ── INTERNET ROW ──────────────────────────────────────────────────────────
    inet_cx = W // 2
    inet_cy = inet_y + INTERNET_H // 2
    # subtle background band
    a(f'<rect x="0" y="{inet_y}" width="{W}" height="{INTERNET_H}" fill="#0c1118"/>')
    a(f'<ellipse cx="{inet_cx}" cy="{inet_cy}" rx="90" ry="26" '
      f'fill="none" stroke="{C["border"]}" stroke-width="1.5"/>')
    a(f'<text x="{inet_cx}" y="{inet_cy+5}" text-anchor="middle" '
      f'fill="{C["dim"]}" font-size="11" letter-spacing="3">INTERNET</text>')

    # connectors: top zone → internet ellipse
    for zone_cx in [saas_x + SAAS_W // 2, pub_x + PUB_W // 2]:
        a(f'<line x1="{zone_cx}" y1="{top_y + top_h}" x2="{inet_cx}" y2="{inet_cy - 26}" '
          f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="3,3"/>')
    # internet → home lab
    a(f'<line x1="{inet_cx}" y1="{inet_cy + 26}" x2="{inet_cx}" y2="{lab_y}" '
      f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="3,3"/>')

    # ── HOME LAB ROW ──────────────────────────────────────────────────────────
    render_zone_box(ZONE_PAD, lab_y, LAB_W, lab_h, C["lab"], "🏠 HOME LAB")
    if lab:
        render_device_rows(lab, ZONE_PAD, LAB_W, lab_y + ZONE_HDR + 8, C["lab"])

    # ── Physical cables ────────────────────────────────────────────────────────
    for (na, nb_) in cables:
        if na in pos_index and nb_ in pos_index:
            ax, ay = pos_index[na]
            bx, by = pos_index[nb_]
            conn_lines.append(
                f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '
                f'stroke="{C["cable"]}" stroke-width="1.5" opacity="0.7"/>'
            )

    # ── Logical tunnels ───────────────────────────────────────────────────────
    for (na, nb_, label, style) in tunnels:
        if na in pos_index and nb_ in pos_index:
            ax, ay = pos_index[na]
            bx, by = pos_index[nb_]
            color  = C["acc"] if style == "wireguard" else C["dim"]
            dash   = "6,4"    if style == "wireguard" else "3,4"
            mid_x, mid_y = (ax + bx) // 2, (ay + by) // 2
            conn_lines.append(
                f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '
                f'stroke="{color}" stroke-width="1.5" stroke-dasharray="{dash}" opacity="0.9"/>'
            )
            lk = "🔒 " if style == "wireguard" else ""
            conn_lines.append(
                f'<rect x="{mid_x-38}" y="{mid_y-9}" width="76" height="16" rx="2" '
                f'fill="{C["bg"]}" stroke="{color}" stroke-width="1"/>'
            )
            conn_lines.append(
                f'<text x="{mid_x}" y="{mid_y+3}" text-anchor="middle" '
                f'fill="{color}" font-size="7" letter-spacing="0.5">{lk}{label}</text>'
            )

    # Insert connection lines right after background rect (index 2 = after <svg> + <rect>)
    if conn_lines:
        svg.insert(2, "\n".join(conn_lines))

    a('</svg>')
    return "\n".join(svg)

# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    pub, lab, cables, tunnels, raw_count = fetch_all()
    now    = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    source = "netbox"

    if not pub and not lab:
        print("[WARN] No tagged devices – using static fallback.")
        pub, lab, cables = FALLBACK_PUBLIC, FALLBACK_HOMELAB, []
        tunnels = LOGICAL_TUNNELS_FALLBACK
        source = "static"

    total_vms = sum(len(n.get("vms", [])) for n in lab)
    os.makedirs("assets", exist_ok=True)

    svg_str = build_svg(SAAS_NODES, pub, lab, cables, tunnels)
    with open(OUTPUT_SVG, "w", encoding="utf-8") as f:
        f.write(svg_str)
    print(f"[OK] {OUTPUT_SVG}  saas={len(SAAS_NODES)}  pub={len(pub)}  lab={len(lab)}  "
          f"vms={total_vms}  cables={len(cables)}  tunnels={len(tunnels)}")

    try:
        import cairosvg
        cairosvg.svg2png(url=OUTPUT_SVG, write_to=OUTPUT_PNG, output_width=1920)
        print(f"[OK] {OUTPUT_PNG}")
    except ImportError:
        print("[INFO] cairosvg not available – PNG skipped.")

    meta = {
        "generated_at":   now,
        "source":         source,
        "saas_count":     len(SAAS_NODES),
        "device_count":   raw_count,
        "pub_nodes":      len(pub),
        "lab_nodes":      len(lab),
        "vm_count":       total_vms,
        "cable_count":    len(cables),
        "tunnel_count":   len(tunnels),
        "tunnel_source":  "netbox" if tunnels and tunnels is not LOGICAL_TUNNELS_FALLBACK else "fallback",
        "filter_tags": {
            "public":  TAG_PUBLIC,
            "homelab": TAG_HOMELAB,
            "exclude": TAG_EXCLUDE,
            "no_vms":  TAG_NO_VMS,
        },
    }
    with open(OUTPUT_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"[OK] {OUTPUT_META}  source={source}")


if __name__ == "__main__":
    main()
──────────────────────────────────────────────────────────────────────────────
  NETBOX_URL    – https://yourinstance.netboxcloud.com
  NETBOX_TOKEN  – read-only API token

OUTPUTS
──────────────────────────────────────────────────────────────────────────────
  assets/diagram.svg
  assets/diagram.png   (optional, requires cairosvg)
  assets/diagram_meta.json
"""

import os
import sys
import json
import datetime
import textwrap
import urllib.request
import urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
NETBOX_URL   = os.environ.get("NETBOX_URL", "").rstrip("/")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN", "")

OUTPUT_SVG  = "assets/diagram.svg"
OUTPUT_PNG  = "assets/diagram.png"
OUTPUT_META = "assets/diagram_meta.json"

# ── Tag slugs (create once in Netbox → Extras → Tags) ────────────────────────
TAG_PUBLIC  = "diagram-public"
TAG_HOMELAB = "diagram-homelab"
TAG_EXCLUDE = "diagram-exclude"

# ── Logical / overlay tunnels (not modelled as cables in Netbox) ──────────────
# Format: (device_name_A, device_name_B, label, style)
# style: "wireguard" | "logical"
LOGICAL_TUNNELS: list[tuple[str, str, str, str]] = [
    ("VPS nginx", "Unifi Gateway", "WireGuard VPN", "wireguard"),
    # Add more here, e.g.:
    # ("Proxmox Node 1", "Proxmox Node 2", "Cluster Sync", "logical"),
]

# ── Static fallback (used when Netbox is not configured / no tagged devices) ──
FALLBACK_PUBLIC = [
    {"name": "VPS · Monitoring",  "detail": "Nagios Core",        "status": "active"},
    {"name": "VPS nginx",          "detail": "Reverse Proxy",      "status": "active"},
    {"name": "Netbox Cloud",       "detail": "IPAM / DCIM (SaaS)", "status": "active"},
]
FALLBACK_HOMELAB = [
    {"name": "Unifi Gateway",      "detail": "WireGuard Endpoint", "status": "active"},
    {"name": "Proxmox Cluster",    "detail": "VMs + LXC",          "status": "active"},
    {"name": "Ansible · RasPi",    "detail": "Control Node",       "status": "active"},
    {"name": "Synology NAS",       "detail": "Docker / n8n",       "status": "active"},
    {"name": "Cisco Switch",       "detail": "VLAN Segmentation",  "status": "active"},
    {"name": "Cisco IP Phones",    "detail": "Webex Calling/SIP",  "status": "active"},
    {"name": "2N Intercom",        "detail": "Door Comm.",          "status": "active"},
    {"name": "2N Access Mgr",      "detail": "Access Control",     "status": "active"},
]
FALLBACK_CABLES: list[tuple[str, str]] = []  # no physical cables in fallback

# ── Netbox API ────────────────────────────────────────────────────────────────

def nb_get(path: str) -> list:
    """Paginated GET from Netbox REST API. Returns all results or []."""
    if not NETBOX_URL or not NETBOX_TOKEN:
        return []
    url: str | None = f"{NETBOX_URL}/api{path}?limit=200"
    results: list = []
    while url:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Token {NETBOX_TOKEN}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
                results.extend(data.get("results", []))
                url = data.get("next")
        except urllib.error.URLError as exc:
            print(f"[WARN] Netbox API error ({path}): {exc}", file=sys.stderr)
            break
    return results


# ── Filtering / classification ─────────────────────────────────────────────────

def _tag_slugs(obj: dict) -> set[str]:
    return {t.get("slug", "") for t in (obj.get("tags") or [])}


def _role_label(d: dict) -> str:
    return ((d.get("device_role") or {}).get("name") or "").title()


def fetch_and_classify() -> tuple[list[dict], list[dict], list[tuple[str,str]], int]:
    """
    Returns:
        pub_nodes   – devices tagged diagram-public
        lab_nodes   – devices tagged diagram-homelab
        cables      – list of (device_name_A, device_name_B) from Netbox cables
        raw_count   – total devices fetched from Netbox
    """
    print("[INFO] Fetching /dcim/devices/ …")
    raw_devices = nb_get("/dcim/devices/")

    pub_nodes: list[dict] = []
    lab_nodes: list[dict] = []
    # Map: device id → name (for cable resolution)
    id_to_name: dict[int, str] = {}

    for d in raw_devices:
        tags   = _tag_slugs(d)
        dev_id = d.get("id")
        name   = d.get("name") or "unnamed"

        if dev_id:
            id_to_name[dev_id] = name

        # Exclude wins over everything
        if TAG_EXCLUDE in tags:
            continue

        in_pub = TAG_PUBLIC  in tags
        in_lab = TAG_HOMELAB in tags

        if not in_pub and not in_lab:
            continue  # not tagged → skip

        node = {
            "name":   name,
            "detail": _role_label(d),
            "status": (d.get("status") or {}).get("value", "active"),
            "id":     dev_id,
        }
        if in_pub:
            pub_nodes.append(node)
        if in_lab:
            lab_nodes.append(node)

    print(f"[INFO] Tagged devices → public={len(pub_nodes)}, homelab={len(lab_nodes)} "
          f"/ {len(raw_devices)} total")

    # ── Cables ────────────────────────────────────────────────────────────────
    print("[INFO] Fetching /dcim/cables/ …")
    raw_cables = nb_get("/dcim/cables/")
    all_tagged_names = {n["name"] for n in pub_nodes} | {n["name"] for n in lab_nodes}
    cables: list[tuple[str, str]] = []

    for cable in raw_cables:
        # Each cable has a_terminations and b_terminations (list of objects)
        def _extract_device_name(terminations: list) -> str | None:
            for t in (terminations or []):
                obj = t.get("object") or {}
                # Termination can be an interface; get its device
                dev = obj.get("device") or {}
                n = dev.get("name") or obj.get("name")
                if n and n in all_tagged_names:
                    return n
            return None

        name_a = _extract_device_name(cable.get("a_terminations") or [])
        name_b = _extract_device_name(cable.get("b_terminations") or [])

        if name_a and name_b and name_a != name_b:
            cables.append((name_a, name_b))

    if raw_cables:
        print(f"[INFO] Cables → {len(raw_cables)} total, {len(cables)} between tagged devices")

    return pub_nodes, lab_nodes, cables, len(raw_devices)


# ── SVG layout helpers ────────────────────────────────────────────────────────

C = {
    "bg":    "#0a0e14", "border": "#1e2a3a",
    "pub":   "#ff8c42", "lab":    "#00e5a0",
    "wg":    "#00d4ff", "dim":    "#5a7a94",
    "head":  "#e8f4ff", "acc":    "#00d4ff",
    "webex": "#0077a8", "off":    "#3a4a5a",
    "cable": "#2a3a4a",
}
CW, CH, HGAP, VGAP = 148, 60, 14, 10


def _wrap(t: str, w: int = 18) -> list[str]:
    return textwrap.wrap(t, w) or [t]


def _layout(n: int, zone_x: int, zone_w: int) -> list[tuple[int, int]]:
    """Returns list of (card_x, row_y_offset) for n cards in up to 2 rows."""
    max_row = max(1, (zone_w + HGAP) // (CW + HGAP))
    r1 = n if n <= max_row else (n + 1) // 2
    r2 = n - r1

    def row_xs(count: int, dy: int) -> list[tuple[int, int]]:
        total = count * CW + (count - 1) * HGAP
        x0 = zone_x + max(0, (zone_w - total) // 2)
        return [(x0 + i * (CW + HGAP), dy) for i in range(count)]

    return row_xs(r1, 0) + (row_xs(r2, CH + VGAP) if r2 > 0 else [])


def _zone_h(n: int, zone_w: int) -> int:
    max_row = max(1, (zone_w + HGAP) // (CW + HGAP))
    rows = 1 if n <= max_row else 2
    return CH * rows + VGAP * (rows - 1) + 56


def _card_center(x: int, y: int) -> tuple[int, int]:
    return x + CW // 2, y + CH // 2


# ── SVG builder ───────────────────────────────────────────────────────────────

def build_svg(
    pub: list[dict],
    lab: list[dict],
    cables: list[tuple[str, str]],
    tunnels: list[tuple[str, str, str, str]],
) -> str:
    W, PAD = 900, 32
    ZW    = W - 2 * PAD
    pub_y = 82
    pub_h = _zone_h(max(len(pub), 1), ZW)
    lab_y = pub_y + pub_h + 44
    lab_h = _zone_h(max(len(lab), 1), ZW)
    H     = lab_y + lab_h + PAD + 8

    lines: list[str] = []
    a = lines.append

    a(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
      f'font-family="IBM Plex Mono, monospace">')
    a(f'<rect width="{W}" height="{H}" fill="{C["bg"]}"/>')

    # Internet ellipse
    mx = W // 2
    a(f'<ellipse cx="{mx}" cy="38" rx="80" ry="24" fill="none" '
      f'stroke="{C["border"]}" stroke-width="1.5"/>')
    a(f'<text x="{mx}" y="43" text-anchor="middle" fill="{C["dim"]}" '
      f'font-size="10" letter-spacing="2">INTERNET</text>')
    a(f'<line x1="{mx}" y1="62" x2="{mx}" y2="{pub_y}" '
      f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="3,3"/>')

    # Webex Cloud badge
    a(f'<rect x="{W-158}" y="10" width="138" height="48" rx="2" '
      f'fill="{C["bg"]}" stroke="{C["webex"]}" stroke-width="1"/>')
    a(f'<text x="{W-89}" y="30" text-anchor="middle" fill="{C["acc"]}" '
      f'font-size="9" font-weight="600">Webex Cloud</text>')
    a(f'<text x="{W-89}" y="46" text-anchor="middle" fill="{C["dim"]}" '
      f'font-size="7">Hybrid Services · Calling</text>')
    a(f'<line x1="{W-158}" y1="34" x2="{mx+80}" y2="34" '
      f'stroke="{C["webex"]}" stroke-width="1" stroke-dasharray="2,3"/>')

    # Build position index: device name → (cx, cy) for connection drawing
    pos_index: dict[str, tuple[int, int]] = {}

    def render_zone(
        nodes: list[dict],
        zy: int,
        zh: int,
        color: str,
        label: str,
        card_y_base: int,
    ) -> None:
        a(f'<rect x="{PAD}" y="{zy}" width="{ZW}" height="{zh}" rx="2" '
          f'fill="none" stroke="{color}" stroke-width="1" stroke-dasharray="4,4"/>')
        a(f'<text x="{PAD+10}" y="{zy+15}" fill="{color}" '
          f'font-size="8" letter-spacing="2">{label}</text>')

        positions = _layout(len(nodes), PAD, ZW)
        for idx, (cx_, dy) in enumerate(positions):
            node   = nodes[idx]
            cy_    = card_y_base + dy
            active = node.get("status", "active") == "active"
            bdr    = color if active else C["off"]

            # Store center for connection lines
            ccx, ccy = _card_center(cx_, cy_)
            pos_index[node["name"]] = (ccx, ccy)

            a(f'<rect x="{cx_}" y="{cy_}" width="{CW}" height="{CH}" rx="2" '
              f'fill="{C["bg"]}" stroke="{bdr}" stroke-width="1"/>')
            for j, ln in enumerate(_wrap(node["name"])[:2]):
                a(f'<text x="{ccx}" y="{cy_+18+j*13}" text-anchor="middle" '
                  f'fill="{C["head"]}" font-size="9" font-weight="600">{ln}</text>')
            if det := node.get("detail", ""):
                a(f'<text x="{ccx}" y="{cy_+CH-9}" text-anchor="middle" '
                  f'fill="{C["dim"]}" font-size="7">{det[:26]}</text>')

    # Render zones
    pub_card_y = pub_y + 36
    render_zone(pub, pub_y, pub_h, C["pub"], "☁ PUBLIC INFRASTRUCTURE", pub_card_y)

    a(f'<line x1="{mx}" y1="{pub_y+pub_h}" x2="{mx}" y2="{lab_y}" '
      f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="3,3"/>')

    lab_card_y = lab_y + 36
    render_zone(lab, lab_y, lab_h, C["lab"], "🏠 HOME LAB", lab_card_y)

    # ── Physical cables (solid lines, drawn AFTER zones so cards are on top) ──
    # We need a second pass: draw lines first, then re-render cards on top.
    # Simplest: draw cables before zone boxes (they'll be under card rects).
    # Because SVG is painters-model, we insert cable lines before zone rects.
    # Workaround: collect cable SVG lines and prepend to output after zones.
    cable_lines: list[str] = []
    for (na, nb) in cables:
        if na in pos_index and nb in pos_index:
            ax, ay = pos_index[na]
            bx, by = pos_index[nb]
            cable_lines.append(
                f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '
                f'stroke="{C["cable"]}" stroke-width="1.5" opacity="0.7"/>'
            )

    # ── Logical tunnels (dashed, with label) ──────────────────────────────────
    for (na, nb, label, style) in tunnels:
        if na in pos_index and nb in pos_index:
            ax, ay = pos_index[na]
            bx, by = pos_index[nb]
            color   = C["wg"] if style == "wireguard" else C["dim"]
            dash    = "6,4" if style == "wireguard" else "3,4"
            mid_x   = (ax + bx) // 2
            mid_y   = (ay + by) // 2
            cable_lines.append(
                f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '
                f'stroke="{color}" stroke-width="1.5" stroke-dasharray="{dash}" opacity="0.85"/>'
            )
            # Tunnel label badge
            cable_lines.append(
                f'<rect x="{mid_x-36}" y="{mid_y-9}" width="72" height="16" rx="2" '
                f'fill="{C["bg"]}" stroke="{color}" stroke-width="1"/>'
            )
            lock = "🔒 " if style == "wireguard" else ""
            cable_lines.append(
                f'<text x="{mid_x}" y="{mid_y+3}" text-anchor="middle" '
                f'fill="{color}" font-size="7" letter-spacing="0.5">{lock}{label}</text>'
            )

    # Insert cable lines just before the closing tag
    if cable_lines:
        lines.insert(3, "\n".join(cable_lines))  # after background rect

    a('</svg>')
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    pub, lab, cables, raw_count = fetch_and_classify()
    now    = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    source = "netbox"

    if not pub and not lab:
        print("[WARN] No tagged devices found – using static fallback data.")
        pub    = FALLBACK_PUBLIC
        lab    = FALLBACK_HOMELAB
        cables = FALLBACK_CABLES
        source = "static"

    os.makedirs("assets", exist_ok=True)

    svg = build_svg(pub, lab, cables, LOGICAL_TUNNELS)
    with open(OUTPUT_SVG, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"[OK] {OUTPUT_SVG}  pub={len(pub)}  lab={len(lab)}  "
          f"cables={len(cables)}  tunnels={len(LOGICAL_TUNNELS)}")

    try:
        import cairosvg
        cairosvg.svg2png(url=OUTPUT_SVG, write_to=OUTPUT_PNG, output_width=1800)
        print(f"[OK] {OUTPUT_PNG}")
    except ImportError:
        print("[INFO] cairosvg not installed – PNG export skipped.")

    meta = {
        "generated_at":    now,
        "source":          source,
        "device_count":    raw_count,
        "pub_nodes":       len(pub),
        "lab_nodes":       len(lab),
        "cable_count":     len(cables),
        "tunnel_count":    len(LOGICAL_TUNNELS),
        "filter_tags": {
            "public":  TAG_PUBLIC,
            "homelab": TAG_HOMELAB,
            "exclude": TAG_EXCLUDE,
        },
    }
    with open(OUTPUT_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"[OK] {OUTPUT_META}  source={source}")


if __name__ == "__main__":
    main()
