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

# Root device for Home Lab topology BFS (first in diagram, all others sorted by cable distance)
HOMELAB_ROOT = "UXG-Fiber"
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

def nb_get(path: str, params: dict | None = None) -> list:
    """Paginated GET. path should NOT contain '?'. Extra params go in `params`."""
    if not NETBOX_URL or not NETBOX_TOKEN:
        return []
    base = f"{NETBOX_URL}/api{path}"
    p = {"limit": "200"}
    if params:
        p.update(params)
    query = "&".join(f"{k}={v}" for k, v in p.items())
    url: str | None = f"{base}?{query}"
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

def _make_node(d: dict, tags: set[str], source: str = "device") -> dict:
    """Build a unified node dict from a Netbox device or VM record."""
    role_field = "device_role" if source == "device" else "role"
    return {
        "name":        d.get("name") or "unnamed",
        "description": d.get("description") or _str(d.get(role_field), "name"),
        "status":      _str(d.get("status"), "value") or "active",
        "cluster_id":  (d.get("cluster") or {}).get("id"),
        "cluster_name":(d.get("cluster") or {}).get("name") or "",
        "no_vms":      TAG_NO_VMS in tags,
        "source":      source,   # "device" or "vm"
        "vms":         [],
    }

# ══════════════════════════════════════════════════════════════════════════════
#  Fetch & classify
# ══════════════════════════════════════════════════════════════════════════════

def fetch_all() -> tuple[list[dict], list[dict], list[tuple[str,str]], list[tuple[str,str,str,str]], int]:

    # ── Step 1: Fetch tagged DEVICES ─────────────────────────────────────────
    print("[INFO] Fetching devices tagged diagram-public …")
    raw_pub_dev  = nb_get("/dcim/devices/", {"tag": TAG_PUBLIC})
    print("[INFO] Fetching devices tagged diagram-homelab …")
    raw_lab_dev  = nb_get("/dcim/devices/", {"tag": TAG_HOMELAB})

    # ── Step 2: Fetch tagged VMs (VPS live here!) ─────────────────────────────
    print("[INFO] Fetching VMs tagged diagram-public …")
    raw_pub_vm   = nb_get("/virtualization/virtual-machines/", {"tag": TAG_PUBLIC})
    print("[INFO] Fetching VMs tagged diagram-homelab …")
    raw_lab_vm   = nb_get("/virtualization/virtual-machines/", {"tag": TAG_HOMELAB})

    # ── Step 3: All devices for cable adjacency resolution ────────────────────
    print("[INFO] Fetching all devices for cable/topology resolution …")
    raw_all_dev  = nb_get("/dcim/devices/")
    print("[INFO] Fetching all VMs for cluster mapping …")
    raw_all_vm   = nb_get("/virtualization/virtual-machines/")

    # Debug: show what tag slugs actually exist
    all_tag_slugs: set[str] = set()
    for d in raw_all_dev + raw_all_vm:
        for t in (d.get("tags") or []):
            all_tag_slugs.add(t.get("slug", ""))
    print(f"[DEBUG] All tag slugs in Netbox (devices+VMs): {sorted(all_tag_slugs)}")
    print(f"[DEBUG] diagram-public  → {len(raw_pub_dev)} devices, {len(raw_pub_vm)} VMs: "
          f"{[d.get('name') for d in raw_pub_dev + raw_pub_vm]}")
    print(f"[DEBUG] diagram-homelab → {len(raw_lab_dev)} devices, {len(raw_lab_vm)} VMs: "
          f"{[d.get('name') for d in raw_lab_dev + raw_lab_vm]}")

    # ── Step 4: Build pub_nodes (devices + VMs tagged diagram-public) ─────────
    pub_nodes: list[dict] = []
    seen_pub: set[str] = set()
    for d in raw_pub_dev:
        tags = _tags(d)
        if TAG_EXCLUDE in tags or d.get("name") in seen_pub:
            continue
        seen_pub.add(d["name"])
        pub_nodes.append(_make_node(d, tags, source="device"))
    for d in raw_pub_vm:
        tags = _tags(d)
        if TAG_EXCLUDE in tags or d.get("name") in seen_pub:
            continue
        seen_pub.add(d["name"])
        pub_nodes.append(_make_node(d, tags, source="vm"))
    pub_nodes.sort(key=lambda n: n["name"])

    # ── Step 5: Build lab_nodes from tagged DEVICES only ─────────────────────
    # Homelab devices are physical; their VMs come from cluster membership,
    # not from the diagram-homelab tag on individual VMs.
    lab_nodes_raw: list[dict] = []
    seen_lab: set[str] = set()
    for d in raw_lab_dev:
        tags = _tags(d)
        if TAG_EXCLUDE in tags or d.get("name") in seen_lab:
            continue
        seen_lab.add(d["name"])
        lab_nodes_raw.append(_make_node(d, tags, source="device"))

    # Also include VMs explicitly tagged diagram-homelab (e.g. a standalone VM
    # that is not part of a Proxmox cluster visible as a device)
    for d in raw_lab_vm:
        tags = _tags(d)
        if TAG_EXCLUDE in tags or d.get("name") in seen_lab:
            continue
        # Only add if not already a child of a cluster host we have
        seen_lab.add(d["name"])
        lab_nodes_raw.append(_make_node(d, tags, source="vm"))

    print(f"[INFO] Classified → public={len(pub_nodes)}, homelab={len(lab_nodes_raw)}")

    # ── Step 6: Attach VMs to clusters, shared across all nodes in cluster ────
    # cluster_id → { name, vms[] }
    cluster_info: dict[int, dict] = {}
    for vm in raw_all_vm:
        cid   = (vm.get("cluster") or {}).get("id")
        cname = (vm.get("cluster") or {}).get("name") or f"cluster-{cid}"
        if cid:
            vm_tags = _tags(vm)
            if TAG_EXCLUDE not in vm_tags:
                if cid not in cluster_info:
                    cluster_info[cid] = {"name": cname, "vms": []}
                cluster_info[cid]["vms"].append({
                    "name":        vm.get("name") or "unnamed-vm",
                    "description": vm.get("description") or _str(vm.get("role"), "name"),
                    "status":      _str(vm.get("status"), "value") or "active",
                })

    # Sort VMs within each cluster
    for ci in cluster_info.values():
        ci["vms"].sort(key=lambda v: v["name"])

    # Group lab nodes by cluster_id to find which nodes share a cluster
    # cluster_id → [node, node, ...]
    cluster_nodes: dict[int, list[dict]] = defaultdict(list)
    for node in lab_nodes_raw:
        if node["cluster_id"]:
            cluster_nodes[node["cluster_id"]].append(node)

    # Assign VMs only to the FIRST node of each cluster (alphabetically by name)
    # Other nodes in the same cluster get vms=[] and a cluster_sibling flag
    for cid, members in cluster_nodes.items():
        members_sorted = sorted(members, key=lambda n: n["name"])
        primary = members_sorted[0]
        ci = cluster_info.get(cid, {})
        if not primary["no_vms"]:
            primary["vms"]          = ci.get("vms", [])
            primary["cluster_name"] = ci.get("name", "")
            primary["cluster_primary"] = True
        for sibling in members_sorted[1:]:
            sibling["cluster_sibling_of"] = primary["name"]
            sibling["cluster_name"]       = ci.get("name", "")
        if ci.get("vms"):
            print(f"  → cluster '{ci['name']}': {len(ci['vms'])} VMs "
                  f"(primary host: {primary['name']})")

    # ── Step 7: Cables → adjacency graph for BFS topology sort ───────────────
    print("[INFO] Fetching /dcim/cables/ …")
    raw_cables   = nb_get("/dcim/cables/")
    all_dev_names = {d.get("name") for d in raw_all_dev if d.get("name")}
    tagged_names  = {n["name"] for n in pub_nodes} | {n["name"] for n in lab_nodes_raw}
    cables: list[tuple[str, str]] = []
    adj: dict[str, set[str]] = defaultdict(set)

    def _dev_name_from_terms(terms: list) -> str | None:
        for t in (terms or []):
            obj = t.get("object") or {}
            dev = obj.get("device") or {}
            n   = dev.get("name") or obj.get("name") or ""
            if n in all_dev_names:
                return n
        return None

    for cable in raw_cables:
        na  = _dev_name_from_terms(cable.get("a_terminations") or [])
        nb_ = _dev_name_from_terms(cable.get("b_terminations") or [])
        if na and nb_ and na != nb_:
            adj[na].add(nb_)
            adj[nb_].add(na)
            if na in tagged_names and nb_ in tagged_names:
                cables.append((na, nb_))

    print(f"[INFO] Cables: {len(raw_cables)} total, {len(cables)} between tagged devices")

    # ── Step 8: BFS topology sort for Home Lab ────────────────────────────────
    lab_nodes = _topo_sort(lab_nodes_raw, adj, root_name=HOMELAB_ROOT)

    # ── Step 9: VPN Tunnels ───────────────────────────────────────────────────
    print("[INFO] Fetching /vpn/tunnels/ …")
    raw_tunnels = nb_get("/vpn/tunnels/")
    tunnels: list[tuple[str, str, str, str]] = []

    if raw_tunnels:
        print(f"[INFO] Found {len(raw_tunnels)} VPN tunnel(s)")
        all_terms = nb_get("/vpn/tunnel-terminations/")
        terms_by_tunnel: dict[int, list[dict]] = defaultdict(list)
        for term in all_terms:
            tid_ = (term.get("tunnel") or {}).get("id")
            if tid_:
                terms_by_tunnel[tid_].append(term)
        for t in raw_tunnels:
            tid   = t.get("id")
            label = t.get("name") or "VPN Tunnel"
            encap = _str(t.get("encapsulation"), "value").lower()
            style = "wireguard" if "wireguard" in encap else "logical"
            dev_names: list[str] = []
            for term in terms_by_tunnel.get(tid, []):
                # termination points to an Interface object which has a .device
                obj  = term.get("termination") or {}
                # obj is the Interface: obj["device"]["name"] is what we want
                dev  = obj.get("device") or {}
                name = dev.get("name") or ""
                # Never fall back to obj["name"] – that would be the interface name (e.g. "wg0")
                if name and name not in dev_names:
                    dev_names.append(name)
            if len(dev_names) >= 2:
                tunnels.append((dev_names[0], dev_names[1], label, style))
                print(f"  -> '{label}' ({style}): {dev_names[0]} <-> {dev_names[1]}")
            else:
                print(f"  [WARN] '{label}': {len(dev_names)} endpoint(s) – raw terminations: "
                      f"{[t.get('termination') for t in terms_by_tunnel.get(tid, [])]}")
    else:
        print("[INFO] No VPN tunnels in Netbox – using fallback")

    if not tunnels:
        tunnels = LOGICAL_TUNNELS_FALLBACK

    total_raw = len(raw_all_dev) + len(raw_all_vm)
    return pub_nodes, lab_nodes, cables, tunnels, total_raw


def _topo_sort(
    nodes: list[dict],
    adj: dict[str, set[str]],
    root_name: str,
) -> list[dict]:
    """
    BFS from root_name through cable adjacency.
    Assigns a 'topo_level' (0, 1, 2, …) to each node so the SVG renderer
    can place them in distinct rows.  Nodes not reachable via cables get
    level = max_level + 1 and are appended alphabetically.
    Returns nodes ordered by (topo_level, name).
    """
    node_map = {n["name"]: n for n in nodes}

    # Pick root
    if root_name in node_map:
        start = root_name
    elif nodes:
        start = sorted(node_map.keys())[0]
        print(f"[WARN] Root '{root_name}' not in homelab nodes – using '{start}'")
    else:
        return nodes

    # BFS – track which level each node lands on
    level_of: dict[str, int] = {start: 0}
    queue: list[str] = [start]
    visited: set[str] = {start}

    while queue:
        current = queue.pop(0)
        cur_level = level_of[current]
        # Only traverse to neighbours that are tagged homelab nodes
        neighbours = sorted(
            adj.get(current, set()) & node_map.keys() - visited
        )
        for nb_ in neighbours:
            visited.add(nb_)
            level_of[nb_] = cur_level + 1
            queue.append(nb_)

    # Assign levels; unreachable nodes go one level below deepest reached
    max_level = max(level_of.values()) if level_of else 0
    for name in node_map:
        if name not in level_of:
            level_of[name] = max_level + 1

    # Stamp level onto each node dict for the renderer
    for node in nodes:
        node["topo_level"] = level_of[node["name"]]

    ordered = sorted(nodes, key=lambda n: (n["topo_level"], n["name"]))
    levels_summary = {}
    for n in ordered:
        lv = n["topo_level"]
        levels_summary.setdefault(lv, []).append(n["name"])
    for lv, names in sorted(levels_summary.items()):
        print(f"[INFO] Home Lab level {lv}: {names}")
    return ordered


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

def _xml(text: str) -> str:
    """Escape text for safe embedding in SVG/XML. Removes non-XML-safe characters."""
    # Standard XML entity escaping
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    # Strip characters outside the XML 1.0 legal range
    # (keeps tabs, newlines, printable ASCII and most Unicode, removes surrogates etc.)
    text = "".join(
        c for c in text
        if c == "\t" or c == "\n" or c == "\r"
        or ("\x20" <= c <= "\ud7ff")
        or ("\ue000" <= c <= "\ufffd")
        or ("\U00010000" <= c <= "\U0010ffff")
    )
    return text

def _wrap(t: str, w: int = 18) -> list[str]:
    return textwrap.wrap(_xml(t), w) or [_xml(t)[:w]]

def _desc_lines(t: str, w: int = 24) -> list[str]:
    return textwrap.wrap(_xml(t), w)[:2] if t else []

def _row_xs(count: int, zone_x: int, zone_w: int, cw: int, gap: int) -> list[int]:
    total = count * cw + max(0, count - 1) * gap
    x0 = zone_x + max(0, (zone_w - total) // 2)
    return [x0 + i * (cw + gap) for i in range(count)]

def _lab_rows(nodes: list[dict]) -> list[list[dict]]:
    """Group lab nodes by topo_level into ordered rows."""
    from collections import OrderedDict
    rows: dict[int, list[dict]] = {}
    for n in nodes:
        lv = n.get("topo_level", 0)
        rows.setdefault(lv, []).append(n)
    return [rows[lv] for lv in sorted(rows)]

def _content_h(nodes: list[dict], zone_w: int) -> int:
    """Estimate total height of home lab content (topo rows + cluster boxes + VMs)."""
    if not nodes:
        return DH
    rows = _lab_rows(nodes)
    total = 0
    for row in rows:
        cluster_primaries = [n for n in row if n.get("cluster_primary") and n.get("vms")]
        vm_extra = sum(CLUSTER_PAD * 2 + VM_H + 28 + 14 for _ in cluster_primaries)
        total += DH + CLUSTER_PAD * 2 + 14 + vm_extra + VGAP * 2
    return total

def _zone_h(nodes: list[dict], zone_w: int) -> int:
    return _content_h(nodes, zone_w) + ZONE_HDR + 20

# Extra padding inside cluster box
CLUSTER_PAD = 10

def build_svg(
    saas:    list[dict],
    pub:     list[dict],
    lab:     list[dict],
    cables:  list[tuple[str, str]],
    tunnels: list[tuple[str, str, str, str]],
) -> str:
    W = 1100

    TOP_PAD  = ZONE_PAD
    TOP_GAP  = 16
    SAAS_W   = int((W - 2 * TOP_PAD - TOP_GAP) * 0.42)
    PUB_W    = W - 2 * TOP_PAD - TOP_GAP - SAAS_W
    saas_x   = TOP_PAD
    pub_x    = TOP_PAD + SAAS_W + TOP_GAP
    top_y    = ZONE_PAD

    def _saas_h() -> int:
        row_max = max(1, (SAAS_W + HGAP) // (DW + HGAP))
        rows = (len(saas) + row_max - 1) // row_max if saas else 1
        return rows * DH + (rows - 1) * VGAP * 2 + ZONE_HDR + 20

    saas_h  = _saas_h()
    pub_h   = _zone_h(pub, PUB_W) if pub else DH + ZONE_HDR + 20
    top_h   = max(saas_h, pub_h)
    inet_y  = top_y + top_h + 20
    LAB_W   = W - 2 * ZONE_PAD
    lab_y   = inet_y + INTERNET_H + 20
    lab_h   = _zone_h(lab, LAB_W) if lab else DH + ZONE_HDR + 20
    H       = lab_y + lab_h + ZONE_PAD + 8

    svg: list[str] = []
    a = svg.append
    conn_lines: list[str] = []
    pos_index: dict[str, tuple[int, int]] = {}

    a(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '      f'font-family="IBM Plex Mono, monospace">')
    a(f'<rect width="{W}" height="{H}" fill="{C["bg"]}"/>')

    def render_card(x, y, name, description, bdr, cw=DW, ch=DH):
        ccx, ccy = x + cw // 2, y + ch // 2
        a(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="2" '          f'fill="{C["bg"]}" stroke="{bdr}" stroke-width="1"/>')
        name_lines = _wrap(name, 20)[:2]
        desc_lines = _desc_lines(description, 28)
        name_h = len(name_lines) * 13
        total_h = name_h + (len(desc_lines) * 11 + 4 if desc_lines else 0)
        sy = y + (ch - total_h) // 2 + 12
        for j, ln in enumerate(name_lines):
            a(f'<text x="{ccx}" y="{sy + j*13}" text-anchor="middle" '              f'fill="{C["head"]}" font-size="9" font-weight="600">{ln}</text>')
        dy2 = sy + name_h + 4
        for j, dl in enumerate(desc_lines):
            a(f'<text x="{ccx}" y="{dy2 + j*11}" text-anchor="middle" '              f'fill="{C["dim"]}" font-size="7">{dl}</text>')
        return ccx, ccy

    def render_vm_card(x, y, vm):
        ccx = x + VM_W // 2
        a(f'<rect x="{x}" y="{y}" width="{VM_W}" height="{VM_H}" rx="2" '          f'fill="{C["vm_bg"]}" stroke="{C["vm_bdr"]}" stroke-width="1" stroke-dasharray="2,2"/>')
        for j, ln in enumerate(_wrap(vm["name"], 16)[:2]):
            a(f'<text x="{ccx}" y="{y+14+j*12}" text-anchor="middle" '              f'fill="{C["head"]}" font-size="8" font-weight="600">{ln}</text>')
        if desc := _xml(vm.get("description", "")):
            short = textwrap.shorten(desc, 22)
            a(f'<text x="{ccx}" y="{y+VM_H-8}" text-anchor="middle" '              f'fill="{C["dim"]}" font-size="6">{short}</text>')
        return ccx, y + VM_H // 2

    def render_zone_box(x, y, w, h, color, label):
        a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" '          f'fill="none" stroke="{color}" stroke-width="1" stroke-dasharray="4,4"/>')
        a(f'<text x="{x+10}" y="{y+15}" fill="{color}" font-size="8" '          f'letter-spacing="2">{_xml(label)}</text>')

    # SaaS zone
    render_zone_box(saas_x, top_y, SAAS_W, top_h, C["saas"], "SaaS SERVICES")
    row_max = max(1, (SAAS_W + HGAP) // (DW + HGAP))
    base_y  = top_y + ZONE_HDR + 8
    for idx, node in enumerate(saas):
        row_i = idx // row_max
        col_i = idx  % row_max
        cnt   = min(row_max, len(saas) - row_i * row_max)
        xs    = _row_xs(cnt, saas_x, SAAS_W, DW, HGAP)
        ccx, ccy = render_card(xs[col_i], base_y + row_i * (DH + VGAP * 2),
                                node["name"], node.get("description",""),
                                node.get("color", C["acc"]))
        pos_index[node["name"]] = (ccx, ccy)

    # Public zone
    render_zone_box(pub_x, top_y, PUB_W, top_h, C["pub"], "PUBLIC INFRASTRUCTURE")
    pub_base = top_y + ZONE_HDR + 8
    pub_xs   = _row_xs(len(pub), pub_x, PUB_W, DW, HGAP)
    for j, node in enumerate(pub):
        bdr = C["pub"] if node.get("status","active") == "active" else C["off"]
        ccx, ccy = render_card(pub_xs[j], pub_base, node["name"],
                                node.get("description",""), bdr)
        pos_index[node["name"]] = (ccx, ccy)

    # Internet band
    inet_cx = W // 2
    inet_cy = inet_y + INTERNET_H // 2
    a(f'<rect x="0" y="{inet_y}" width="{W}" height="{INTERNET_H}" fill="#0c1118"/>')
    a(f'<ellipse cx="{inet_cx}" cy="{inet_cy}" rx="90" ry="26" '      f'fill="none" stroke="{C["border"]}" stroke-width="1.5"/>')
    a(f'<text x="{inet_cx}" y="{inet_cy+5}" text-anchor="middle" '      f'fill="{C["dim"]}" font-size="11" letter-spacing="3">INTERNET</text>')
    for zone_cx in [saas_x + SAAS_W // 2, pub_x + PUB_W // 2]:
        a(f'<line x1="{zone_cx}" y1="{top_y+top_h}" x2="{inet_cx}" y2="{inet_cy-26}" '          f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="3,3"/>')
    a(f'<line x1="{inet_cx}" y1="{inet_cy+26}" x2="{inet_cx}" y2="{lab_y}" '      f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="3,3"/>')

    # Home Lab zone
    render_zone_box(ZONE_PAD, lab_y, LAB_W, lab_h, C["lab"], "HOME LAB")

    rendered_vms: set[str] = set()

    def render_lab_rows(nodes, zone_x, zone_w, card_y_base):
        rows   = _lab_rows(nodes)
        row_dy = 0
        for row in rows:
            row_y = card_y_base + row_dy

            # Group: cluster nodes vs solo nodes
            cluster_groups: dict[int, list[dict]] = {}
            solo_nodes: list[dict] = []
            for node in row:
                cid = node.get("cluster_id")
                if cid and (node.get("cluster_primary") or node.get("cluster_sibling_of")):
                    cluster_groups.setdefault(cid, []).append(node)
                else:
                    solo_nodes.append(node)

            # Build render items with widths
            items: list[dict] = []
            for node in solo_nodes:
                items.append({"type": "solo", "nodes": [node], "width": DW})
            for cid, members in cluster_groups.items():
                ms = sorted(members, key=lambda n: n["name"])
                cw = len(ms) * DW + (len(ms)-1) * HGAP + CLUSTER_PAD * 2
                items.append({"type": "cluster", "nodes": ms, "cid": cid, "width": cw})

            total_w = sum(it["width"] for it in items) + max(0, len(items)-1) * HGAP
            cur_x   = zone_x + max(0, (zone_w - total_w) // 2)
            row_vm_h = 0

            for item in items:
                if item["type"] == "solo":
                    node = item["nodes"][0]
                    bdr  = C["lab"] if node.get("status","active") == "active" else C["off"]
                    ccx, ccy = render_card(cur_x, row_y + CLUSTER_PAD + 14,
                                           node["name"], node.get("description",""), bdr)
                    pos_index[node["name"]] = (ccx, ccy)
                    vms = [v for v in node.get("vms",[]) if v["name"] not in rendered_vms]
                    if vms:
                        vm_tw = len(vms)*VM_W + (len(vms)-1)*VM_HGAP
                        vm_x0 = ccx - vm_tw // 2
                        vm_y  = row_y + CLUSTER_PAD + 14 + DH + 16
                        for k, vm in enumerate(vms):
                            vx = vm_x0 + k*(VM_W+VM_HGAP)
                            conn_lines.append(
                                f'<line x1="{ccx}" y1="{row_y+CLUSTER_PAD+14+DH}" '                                f'x2="{vx+VM_W//2}" y2="{vm_y}" '                                f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="2,2"/>')
                            render_vm_card(vx, vm_y, vm)
                            rendered_vms.add(vm["name"])
                        row_vm_h = max(row_vm_h, VM_H + 16)

                else:
                    ms   = item["nodes"]
                    cid  = item["cid"]
                    cn   = ms[0].get("cluster_name", f"cluster-{cid}")
                    bx   = cur_x
                    bw   = item["width"]
                    bh   = DH + CLUSTER_PAD * 2 + 14
                    box_cx = bx + bw // 2

                    # Cluster box
                    a(f'<rect x="{bx}" y="{row_y}" width="{bw}" height="{bh}" rx="3" '                      f'fill="none" stroke="{C["vm_bdr"]}" stroke-width="1" '                      f'stroke-dasharray="3,3"/>')
                    a(f'<text x="{box_cx}" y="{row_y+10}" text-anchor="middle" '                      f'fill="{C["dim"]}" font-size="7" letter-spacing="1">{_xml(cn)}</text>')

                    inner_xs = _row_xs(len(ms), bx+CLUSTER_PAD, bw-CLUSTER_PAD*2, DW, HGAP)
                    for j2, node in enumerate(ms):
                        bdr = C["lab"] if node.get("status","active") == "active" else C["off"]
                        ccx, ccy = render_card(inner_xs[j2], row_y+14,
                                               node["name"], node.get("description",""), bdr)
                        pos_index[node["name"]] = (ccx, ccy)

                    primary = next((n for n in ms if n.get("cluster_primary")), ms[0])
                    vms = [v for v in primary.get("vms",[]) if v["name"] not in rendered_vms]
                    if vms:
                        vm_tw  = len(vms)*VM_W + (len(vms)-1)*VM_HGAP
                        vm_x0  = box_cx - vm_tw // 2
                        vm_y   = row_y + bh + 16
                        box_by = row_y + bh
                        conn_lines.append(
                            f'<line x1="{box_cx}" y1="{box_by}" '                            f'x2="{box_cx}" y2="{vm_y}" '                            f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="2,2"/>')
                        for k, vm in enumerate(vms):
                            vcx = vm_x0 + k*(VM_W+VM_HGAP) + VM_W//2
                            conn_lines.append(
                                f'<line x1="{box_cx}" y1="{vm_y}" '                                f'x2="{vcx}" y2="{vm_y}" '                                f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="2,2"/>')
                            render_vm_card(vm_x0+k*(VM_W+VM_HGAP), vm_y, vm)
                            rendered_vms.add(vm["name"])
                        row_vm_h = max(row_vm_h, bh + VM_H + 16)

                cur_x += item["width"] + HGAP

            row_dy += CLUSTER_PAD*2 + 14 + DH + (row_vm_h + VGAP if row_vm_h else 0) + VGAP*2

    render_lab_rows(lab, ZONE_PAD, LAB_W, lab_y + ZONE_HDR + 8)

    # Physical cables
    for (na, nb_) in cables:
        if na in pos_index and nb_ in pos_index:
            ax, ay = pos_index[na]
            bx, by = pos_index[nb_]
            conn_lines.append(
                f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '                f'stroke="{C["cable"]}" stroke-width="1.5" opacity="0.7"/>')

    # VPN tunnels
    for (na, nb_, label, style) in tunnels:
        if na in pos_index and nb_ in pos_index:
            ax, ay = pos_index[na]
            bx, by = pos_index[nb_]
            color = C["acc"] if style == "wireguard" else C["dim"]
            dash  = "6,4"    if style == "wireguard" else "3,4"
            mx, my = (ax+bx)//2, (ay+by)//2
            conn_lines.append(
                f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '                f'stroke="{color}" stroke-width="1.5" stroke-dasharray="{dash}" opacity="0.9"/>')
            lk = "[WG] " if style == "wireguard" else ""
            conn_lines.append(
                f'<rect x="{mx-42}" y="{my-9}" width="84" height="16" rx="2" '                f'fill="{C["bg"]}" stroke="{color}" stroke-width="1"/>')
            conn_lines.append(
                f'<text x="{mx}" y="{my+3}" text-anchor="middle" '                f'fill="{color}" font-size="7" letter-spacing="0.5">{lk}{_xml(label)}</text>')

    if conn_lines:
        svg.insert(2, "\n".join(conn_lines))

    a('</svg>')
    return "\n".join(svg)


#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    pub, lab, cables, tunnels, raw_count = fetch_all()
    now    = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
  
