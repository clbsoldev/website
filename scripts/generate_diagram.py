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
LOGICAL_TUNNELS_FALLBACK: list[tuple[str, str, str, str, str]] = [
    ("VPS nginx", "Unifi Gateway", "WireGuard VPN", "wireguard", ""),
]

# ══════════════════════════════════════════════════════════════════════════════
#  Config / tags
# ══════════════════════════════════════════════════════════════════════════════

NETBOX_URL   = os.environ.get("NETBOX_URL", "").rstrip("/")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN", "")

OUTPUT_SVG  = "assets/diagram.svg"
OUTPUT_PNG  = "assets/diagram.png"
OUTPUT_META = "assets/diagram_meta.json"

TAG_PUBLIC         = "diagram-public"
TAG_HOMELAB        = "diagram-homelab"
TAG_EXCLUDE        = "diagram-exclude"
TAG_NO_VMS         = "diagram-no-vms"
TAG_CLUSTER_EXCLUDE = "diagram-cluster-exclude"  # skip this cluster in --cluster-diagrams

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
    """Paginated GET. Retries up to 3 times on timeout/connection error."""
    if not NETBOX_URL or not NETBOX_TOKEN:
        return []
    base  = f"{NETBOX_URL}/api{path}"
    p     = {"limit": "200"}
    if params:
        p.update(params)
    query = "&".join(f"{k}={v}" for k, v in p.items())
    url: str | None = f"{base}?{query}"
    results: list   = []

    while url:
        last_exc = None
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Token {NETBOX_TOKEN}",
                        "Accept":        "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                    results.extend(data.get("results", []))
                    url = data.get("next")
                    last_exc = None
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                wait = attempt * 5
                print(f"[WARN] Netbox API attempt {attempt}/3 failed "
                      f"({path}): {exc}  – retrying in {wait}s …",
                      file=sys.stderr)
                import time; time.sleep(wait)

        if last_exc is not None:
            print(f"[ERROR] Netbox API unreachable after 3 attempts: {path}",
                  file=sys.stderr)
            print(f"[ERROR] Target URL (no token): {base}",
                  file=sys.stderr)
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
    # Fetch cluster details (name + description) from Netbox
    print("[INFO] Fetching /virtualization/clusters/ …")
    raw_clusters = nb_get("/virtualization/clusters/")
    cluster_details: dict[int, dict] = {}
    for cl in raw_clusters:
        cid = cl.get("id")
        if cid:
            # custom_fields: look for a field named 'diagram_notes' or 'notes'
            cf = cl.get("custom_fields") or {}
            notes = cf.get("diagram_notes") or cf.get("notes") or ""
            # comments is a Netbox free-text field (may contain Markdown)
            comments = cl.get("comments") or ""
            # Prefer custom_field notes, fall back to comments
            detail_text = notes if notes else comments
            cluster_details[cid] = {
                "name":        cl.get("name") or f"cluster-{cid}",
                "description": cl.get("description") or "",
                "notes":       detail_text,
                "excluded":    TAG_CLUSTER_EXCLUDE in {t.get("slug","") for t in (cl.get("tags") or [])},
            }

    # cluster_id → { name, description, notes, vms[] }
    cluster_info: dict[int, dict] = {}
    for vm in raw_all_vm:
        cid   = (vm.get("cluster") or {}).get("id")
        cname = (vm.get("cluster") or {}).get("name") or f"cluster-{cid}"
        if cid:
            vm_tags = _tags(vm)
            if TAG_EXCLUDE not in vm_tags:
                if cid not in cluster_info:
                    det = cluster_details.get(cid, {})
                    cluster_info[cid] = {
                        "name":        det.get("name") or cname,
                        "description": det.get("description") or "",
                        "notes":       det.get("notes") or "",
                        "vms":         [],
                    }
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
            primary["vms"]              = ci.get("vms", [])
            primary["cluster_name"]     = ci.get("name", "")
            primary["cluster_desc"]     = ci.get("description", "")
            primary["cluster_notes"]    = ci.get("notes", "")
            primary["cluster_excluded"] = ci.get("excluded", False)
            primary["cluster_primary"]  = True
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
            tdesc = t.get("description") or ""
            dev_names: list[str] = []
            for term in terms_by_tunnel.get(tid, []):
                obj  = term.get("termination") or {}
                dev  = obj.get("device") or {}
                name = dev.get("name") or ""
                if not name:
                    vm_ref = obj.get("virtual_machine") or {}
                    name   = vm_ref.get("name") or ""
                if name and name not in dev_names:
                    dev_names.append(name)
            if len(dev_names) >= 2:
                tunnels.append((dev_names[0], dev_names[1], label, style, tdesc))
                print(f"  -> '{label}' ({style}): {dev_names[0]} <-> {dev_names[1]}"
                      f"{' | ' + tdesc if tdesc else ''}")
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
        neighbours = sorted(
            adj.get(current, set()) & node_map.keys() - visited
        )
        for nb_ in neighbours:
            visited.add(nb_)
            level_of[nb_] = cur_level + 1
            node_map[nb_]["_parent"] = current   # store parent for tree layout
            queue.append(nb_)

    # Assign levels; unreachable nodes go one level below deepest reached
    max_level = max(level_of.values()) if level_of else 0
    for name in node_map:
        if name not in level_of:
            level_of[name] = max_level + 1

    # Force cluster siblings onto the same level as their cluster primary
    # (hyp03 may have no cable to hyp01/02 but belongs in the same cluster box)
    for node in nodes:
        sibling_of = node.get("cluster_sibling_of")
        if sibling_of and sibling_of in level_of:
            level_of[node["name"]] = level_of[sibling_of]
            # Also inherit parent from primary so layout places them together
            if "_parent" in node_map.get(sibling_of, {}):
                node["_parent"] = node_map[sibling_of].get("_parent")

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


# ══════════════════════════════════════════════════════════════════════════════
#  SVG constants (layout)
# ══════════════════════════════════════════════════════════════════════════════

DW, DH        = 170, 72    # device card width / height
VM_W, VM_H    = 140, 44    # VM card
HGAP          = 18          # horizontal gap between sibling cards
VGAP          = 12          # vertical gap between rows
VM_HGAP       = 10
CLUSTER_PAD   = 10          # padding inside cluster box
ZONE_HDR      = 20
ZONE_PAD      = 28
INTERNET_H    = 70
ROW_GAP       = 48          # vertical gap between topo rows

# ══════════════════════════════════════════════════════════════════════════════
#  Balanced-tree layout helpers
# ══════════════════════════════════════════════════════════════════════════════

def _lab_rows(nodes: list[dict], zone_w: int = 1044) -> list[list[dict]]:
    """
    Group lab nodes by topo_level into ordered rows.
    If a row's total item width exceeds zone_w, split it into
    multiple sub-rows (each still rendered at a distinct y).
    Each sub-row carries the same topo_level so parent-child
    connectors still work.
    """
    from collections import OrderedDict
    level_nodes: dict[int, list[dict]] = {}
    for n in nodes:
        lv = n.get("topo_level", 0)
        level_nodes.setdefault(lv, []).append(n)

    result: list[list[dict]] = []
    max_w = zone_w - 4 * HGAP   # leave comfortable margin

    for lv in sorted(level_nodes):
        row = level_nodes[lv]
        items = _build_items(row)
        total_w = sum(_item_width(it) for it in items) + max(0, len(items)-1)*HGAP

        if total_w <= max_w:
            result.append(row)
        else:
            # Split items into sub-rows that fit within max_w
            sub: list[dict] = []
            sub_w = 0
            for it in items:
                iw = _item_width(it)
                gap = HGAP if sub else 0
                if sub and sub_w + gap + iw > max_w:
                    # Flush current sub-row
                    sub_nodes = [n for it2 in sub for n in it2["nodes"]]
                    result.append(sub_nodes)
                    sub = [it]
                    sub_w = iw
                else:
                    sub.append(it)
                    sub_w += gap + iw
            if sub:
                sub_nodes = [n for it2 in sub for n in it2["nodes"]]
                result.append(sub_nodes)

    return result


def _item_width(item: dict) -> int:
    """Natural width of a render item (solo card or cluster box)."""
    if item["type"] == "solo":
        return DW
    members = item["nodes"]
    return len(members) * DW + (len(members) - 1) * HGAP + CLUSTER_PAD * 2


def _build_items(row: list[dict], simplify: bool = True) -> list[dict]:
    """Convert a topo row into render items (solo | cluster).
    When simplify=False, all nodes are rendered as solo cards (no cluster box)."""
    if not simplify:
        items = [{"type": "solo", "nodes": [node]} for node in row]
        items.sort(key=lambda it: it["nodes"][0]["name"])
        return items
    cluster_groups: dict[int, list[dict]] = {}
    solo: list[dict] = []
    for node in row:
        cid = node.get("cluster_id")
        if cid and (node.get("cluster_primary") or node.get("cluster_sibling_of")):
            cluster_groups.setdefault(cid, []).append(node)
        else:
            solo.append(node)
    items: list[dict] = []
    for node in solo:
        items.append({"type": "solo", "nodes": [node]})
    for cid, members in cluster_groups.items():
        ms = sorted(members, key=lambda n: n["name"])
        items.append({"type": "cluster", "nodes": ms, "cid": cid})
    items.sort(key=lambda it: it["nodes"][0]["name"])
    return items


def _assign_x_positions(
    rows_items: list[list[dict]],
    zone_cx: int,
    zone_x: int = 28,
    zone_w: int = 1044,
) -> dict[str, int]:
    """
    Balanced-tree horizontal layout with collision resolution.
    Each parent group is centred under its parent, then groups are
    shifted apart if they overlap, while keeping the whole row within
    zone boundaries.
    """
    name_cx: dict[str, int] = {}

    def _place_group(items: list[dict], centre_x: int) -> list[tuple[int,int,dict]]:
        """Return list of (left_x, right_x, item) placed around centre_x."""
        total_w = sum(_item_width(it) for it in items) + max(0, len(items)-1)*HGAP
        cur_x   = centre_x - total_w // 2
        result  = []
        for it in items:
            iw = _item_width(it)
            result.append((cur_x, cur_x + iw, it))
            cur_x += iw + HGAP
        return result

    def _resolve_collisions(
        placed_groups: list[list[tuple[int,int,dict]]]
    ) -> list[list[tuple[int,int,dict]]]:
        """
        Shift groups apart until no two groups overlap.
        Groups are sorted left-to-right by their centre, then pushed
        outward from their preferred positions while respecting zone bounds.
        """
        # Flatten to (centre_x, group_idx, items)
        groups = []
        for gi, grp in enumerate(placed_groups):
            if not grp:
                continue
            left  = grp[0][0]
            right = grp[-1][1]
            gcx   = (left + right) // 2
            groups.append({"gi": gi, "grp": grp, "gcx": gcx,
                           "w": right - left})
        groups.sort(key=lambda g: g["gcx"])

        # Iteratively push overlapping neighbours apart (max 10 passes)
        for _ in range(10):
            changed = False
            for i in range(len(groups) - 1):
                a, b = groups[i], groups[i+1]
                a_right = a["gcx"] + a["w"] // 2
                b_left  = b["gcx"] - b["w"] // 2
                if a_right + HGAP > b_left:
                    overlap = (a_right + HGAP - b_left)
                    # Push both apart equally
                    a["gcx"] -= overlap // 2
                    b["gcx"] += overlap - overlap // 2
                    changed = True
            if not changed:
                break

        # Clamp entire row into zone bounds
        if groups:
            row_left  = groups[0]["gcx"]  - groups[0]["w"] // 2
            row_right = groups[-1]["gcx"] + groups[-1]["w"] // 2
            if row_left < zone_x + HGAP:
                shift = zone_x + HGAP - row_left
                for g in groups: g["gcx"] += shift
            row_right = groups[-1]["gcx"] + groups[-1]["w"] // 2
            if row_right > zone_x + zone_w - HGAP:
                shift = row_right - (zone_x + zone_w - HGAP)
                for g in groups: g["gcx"] -= shift

        # Re-place each group at its resolved centre
        result = [[] for _ in placed_groups]
        for g in groups:
            placed = _place_group(
                [t[2] for t in g["grp"]], g["gcx"]
            )
            result[g["gi"]] = placed
        return result

    for row_idx, items in enumerate(rows_items):
        if row_idx == 0:
            placed = _place_group(items, zone_cx)
            for lx, rx, it in placed:
                icx = (lx + rx) // 2
                it["_cx"] = icx
                it["_w"]  = rx - lx
                for node in it["nodes"]:
                    name_cx[node["name"]] = icx
        else:
            from collections import defaultdict as _dd
            parent_groups_map: dict[int, list[dict]] = _dd(list)
            orphans: list[dict] = []
            for it in items:
                parent_name = it["nodes"][0].get("_parent")
                if parent_name and parent_name in name_cx:
                    pcx = name_cx[parent_name]
                    parent_groups_map[pcx].append(it)
                else:
                    orphans.append(it)

            # Place each group at its preferred parent centre
            placed_groups: list[list[tuple]] = []
            group_keys = sorted(parent_groups_map)
            for pcx in group_keys:
                placed_groups.append(_place_group(parent_groups_map[pcx], pcx))
            if orphans:
                placed_groups.append(_place_group(orphans, zone_cx))

            # Resolve collisions between groups
            resolved = _resolve_collisions(placed_groups)

            # Commit positions
            for grp_placed in resolved:
                for lx, rx, it in grp_placed:
                    icx = (lx + rx) // 2
                    it["_cx"] = icx
                    it["_w"]  = rx - lx
                    for node in it["nodes"]:
                        name_cx[node["name"]] = icx

    # ── Bottom-up pass: re-centre parents over their single child ────────────
    # Only applies when a parent node is the sole occupant on its side AND
    # has exactly one child item — e.g. USW-Lite over MGMT-Pi-Home.
    # Skip if the parent shares its row with siblings that have their own
    # children (moving it would break the sibling spacing).
    for row_idx in range(len(rows_items) - 2, -1, -1):
        parent_row = rows_items[row_idx]
        child_row  = rows_items[row_idx + 1] if row_idx + 1 < len(rows_items) else []

        # Build: parent_name → list of (child_left, child_right) spans
        parent_child_spans: dict[str, list[tuple[int,int]]] = {}
        for it in child_row:
            pname = it["nodes"][0].get("_parent")
            if pname and pname in name_cx:
                cw = it.get("_w", _item_width(it))
                ccx = it.get("_cx", name_cx.get(it["nodes"][0]["name"], 0))
                parent_child_spans.setdefault(pname, []).append(
                    (ccx - cw // 2, ccx + cw // 2)
                )

        # Count how many parents in this row have children
        parents_with_children = [
            it for it in parent_row
            if any(n["name"] in parent_child_spans for n in it["nodes"])
        ]

        for it in parent_row:
            for node in it["nodes"]:
                spans = parent_child_spans.get(node["name"], [])
                if len(spans) != 1:
                    continue  # only move parents with exactly one child item
                # Only move if this parent is the sole parent with children on
                # this side, i.e. no sibling parent is competing for same space
                # Heuristic: skip if >2 parents with children exist in this row
                if len(parents_with_children) > 2:
                    continue
                child_left, child_right = spans[0]
                new_cx = (child_left + child_right) // 2
                it["_cx"] = new_cx
                name_cx[node["name"]] = new_cx

    return name_cx



def _zone_h_lab(nodes: list[dict]) -> int:
    """Estimate home lab zone height."""
    if not nodes:
        return DH + ZONE_HDR + 20
    rows = _lab_rows(nodes, zone_w=1044)
    total = ZONE_HDR + 12
    for row in rows:
        items = _build_items(row)
        row_h = CLUSTER_PAD * 2 + 14 + DH  # cluster box height
        # Check if any item has VMs
        for it in items:
            primary = next((n for n in it["nodes"] if n.get("cluster_primary")), it["nodes"][0])
            if primary.get("vms"):
                row_h += VM_H + 24
                break
        total += row_h + ROW_GAP
    return total + 8


# ══════════════════════════════════════════════════════════════════════════════
#  SVG builder
# ══════════════════════════════════════════════════════════════════════════════

def build_svg(
    saas:     list[dict],
    pub:      list[dict],
    lab:      list[dict],
    cables:   list[tuple[str, str]],
    tunnels:  list[tuple[str, str, str, str]],
    simplify: bool = True,   # True = cluster boxes; False = individual nodes (--no-simplify)
) -> str:
    W = 1100

    TOP_PAD  = ZONE_PAD
    TOP_GAP  = 16
    HALF     = (W - 2 * TOP_PAD - TOP_GAP) // 2
    SAAS_W   = HALF
    PUB_W    = W - 2 * TOP_PAD - TOP_GAP - SAAS_W
    saas_x   = TOP_PAD
    pub_x    = TOP_PAD + SAAS_W + TOP_GAP
    top_y    = ZONE_PAD

    def _grid_h(nodes_list, zone_w):
        if not nodes_list:
            return DH + ZONE_HDR + 20
        row_max = max(1, (zone_w + HGAP) // (DW + HGAP))
        rows    = (len(nodes_list) + row_max - 1) // row_max
        return rows * DH + (rows-1)*(VGAP*2) + ZONE_HDR + 28

    saas_h = _grid_h(saas, SAAS_W)
    pub_h  = _grid_h(pub,  PUB_W)
    top_h  = max(saas_h, pub_h)
    inet_y = top_y + top_h + 20
    LAB_W  = W - 2 * ZONE_PAD
    lab_y  = inet_y + INTERNET_H + 20
    lab_h  = _zone_h_lab(lab)
    H      = lab_y + lab_h + ZONE_PAD + 8

    svg: list[str] = []
    a = svg.append
    conn_lines: list[str] = []
    pos_index:  dict[str, tuple[int,int]] = {}

    a(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
      f'font-family="IBM Plex Mono, monospace">')
    a(f'<rect width="{W}" height="{H}" fill="{C["bg"]}"/>')

    # ── Primitives ─────────────────────────────────────────────────────────────

    def _text_lines(text: str, max_w: int, wrap_w: int) -> list[str]:
        return _wrap(text, wrap_w)[:3]

    def render_card(x, y, name, description, bdr, cw=DW, ch=DH):
        ccx = x + cw // 2
        ccy = y + ch // 2
        a(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="2" '
          f'fill="{C["bg"]}" stroke="{bdr}" stroke-width="1"/>')
        nls = _wrap(name, 22)[:2]
        dls = _desc_lines(description, 30)
        nh  = len(nls) * 13
        th  = nh + (len(dls)*11 + 4 if dls else 0)
        # Centre vertically, but cap so text never overflows card bottom
        sy  = y + max(10, (ch - th) // 2 + 10)
        for j, ln in enumerate(nls):
            a(f'<text x="{ccx}" y="{sy+j*13}" text-anchor="middle" '
              f'fill="{C["head"]}" font-size="9" font-weight="600">{ln}</text>')
        dy2 = sy + nh + 4
        for j, dl in enumerate(dls):
            if dy2 + j*11 < y + ch - 4:   # guard: only render if within card
                a(f'<text x="{ccx}" y="{dy2+j*11}" text-anchor="middle" '
                  f'fill="{C["dim"]}" font-size="7">{dl}</text>')
        return ccx, ccy

    def render_vm_card(x, y, vm):
        ccx    = x + VM_W // 2
        status = vm.get("status", "active")
        bdr    = C["lab"] if status == "active" else C["off"]
        a(f'<rect x="{x}" y="{y}" width="{VM_W}" height="{VM_H}" rx="2" '
          f'fill="{C["vm_bg"]}" stroke="{bdr}" stroke-width="1" '
          f'stroke-dasharray="2,2"/>')
        nls = _wrap(vm["name"], 18)[:2]
        nh  = len(nls) * 12
        dls = _desc_lines(vm.get("description",""), 22)
        th  = nh + (len(dls)*10+3 if dls else 0)
        sy  = y + (VM_H - th) // 2 + 11
        for j, ln in enumerate(nls):
            a(f'<text x="{ccx}" y="{sy+j*12}" text-anchor="middle" '
              f'fill="{C["head"]}" font-size="8" font-weight="600">{ln}</text>')
        dy2 = sy + nh + 3
        for j, dl in enumerate(dls):
            a(f'<text x="{ccx}" y="{dy2+j*10}" text-anchor="middle" '
              f'fill="{C["dim"]}" font-size="6">{dl}</text>')
        return ccx, y + VM_H // 2

    def render_zone_box(x, y, w, h, color, label):
        a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" '
          f'fill="none" stroke="{color}" stroke-width="1" stroke-dasharray="4,4"/>')
        a(f'<text x="{x+10}" y="{y+15}" fill="{color}" font-size="8" '
          f'letter-spacing="2">{_xml(label)}</text>')

    # ── SaaS zone ─────────────────────────────────────────────────────────────
    render_zone_box(saas_x, top_y, SAAS_W, top_h, C["saas"], "SaaS SERVICES")
    row_max  = max(1, (SAAS_W + HGAP) // (DW + HGAP))
    base_y   = top_y + ZONE_HDR + 14
    for idx, node in enumerate(saas):
        ri = idx // row_max
        ci = idx  % row_max
        cnt = min(row_max, len(saas) - ri * row_max)
        total_rw = cnt * DW + (cnt-1) * HGAP
        rx0 = saas_x + (SAAS_W - total_rw) // 2
        ccx, ccy = render_card(
            rx0 + ci*(DW+HGAP), base_y + ri*(DH+VGAP*2),
            node["name"], node.get("description",""),
            node.get("color", C["acc"]))
        pos_index[node["name"]] = (ccx, ccy)

    # ── Public zone ────────────────────────────────────────────────────────────
    render_zone_box(pub_x, top_y, PUB_W, top_h, C["pub"], "PUBLIC INFRASTRUCTURE")
    row_max2 = max(1, (PUB_W + HGAP) // (DW + HGAP))
    base_y2  = top_y + ZONE_HDR + 14
    for idx, node in enumerate(pub):
        ri = idx // row_max2
        ci = idx  % row_max2
        cnt = min(row_max2, len(pub) - ri * row_max2)
        total_rw = cnt * DW + (cnt-1) * HGAP
        rx0 = pub_x + (PUB_W - total_rw) // 2
        bdr = C["pub"] if node.get("status","active") == "active" else C["off"]
        ccx, ccy = render_card(
            rx0 + ci*(DW+HGAP), base_y2 + ri*(DH+VGAP*2),
            node["name"], node.get("description",""), bdr)
        pos_index[node["name"]] = (ccx, ccy)

    # ── Internet band (rendered into svg list directly so it sits BELOW conn_lines) ──
    inet_cx = W // 2
    inet_cy = inet_y + INTERNET_H // 2
    # Store Internet band elements in a separate list – inserted before conn_lines
    inet_elems: list[str] = []
    inet_elems.append(
        f'<rect x="0" y="{inet_y}" width="{W}" height="{INTERNET_H}" '
        f'fill="#0c1118" opacity="0.85"/>')
    inet_elems.append(
        f'<ellipse cx="{inet_cx}" cy="{inet_cy}" rx="90" ry="26" '
        f'fill="none" stroke="{C["border"]}" stroke-width="1.5"/>')
    inet_elems.append(
        f'<text x="{inet_cx}" y="{inet_cy+5}" text-anchor="middle" '
        f'fill="{C["dim"]}" font-size="11" letter-spacing="3">INTERNET</text>')
    # Zone tops → Internet connectors
    for zcx in [saas_x + SAAS_W//2, pub_x + PUB_W//2]:
        inet_elems.append(
            f'<line x1="{zcx}" y1="{top_y+top_h}" x2="{inet_cx}" y2="{inet_cy-26}" '
            f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="3,3"/>')
    # Note: Internet → UXG-Fiber connector is drawn after the lab is rendered
    # (added to conn_lines once UXG card position is known)

    # ── Home Lab: balanced-tree layout ─────────────────────────────────────────
    render_zone_box(ZONE_PAD, lab_y, LAB_W, lab_h, C["lab"], "HOME LAB")

    if lab:
        rows       = _lab_rows(lab, zone_w=LAB_W)
        # When simplify=False, treat every node as solo (no cluster boxes)
        rows_items = [_build_items(row, simplify=simplify) for row in rows]
        zone_cx    = ZONE_PAD + LAB_W // 2

        name_cx = _assign_x_positions(rows_items, zone_cx,
                                       zone_x=ZONE_PAD, zone_w=LAB_W)

        rendered_vms: set[str] = set()
        row_y = lab_y + ZONE_HDR + 14

        for row_idx, items in enumerate(rows_items):
            card_y    = row_y + CLUSTER_PAD + 14
            box_top_y = row_y
            row_has_vms = False

            for it in items:
                iw  = _item_width(it)
                icx = it.get("_cx", zone_cx)
                ix  = icx - iw // 2

                if it["type"] == "solo":
                    node = it["nodes"][0]
                    bdr  = C["lab"] if node.get("status","active") == "active" else C["off"]
                    ccx, ccy = render_card(ix, card_y, node["name"],
                                           node.get("description",""), bdr)
                    pos_index[node["name"]] = (ccx, ccy)

                    # UXG-Fiber → Internet ellipse: from card top centre to ellipse bottom
                    if node["name"] == HOMELAB_ROOT:
                        conn_lines.append(
                            f'<line x1="{ccx}" y1="{card_y}" '
                            f'x2="{inet_cx}" y2="{inet_cy+26}" '
                            f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="3,3"/>')
                        # Remove the generic inet→lab_y line from inet_elems (last element)
                        # by replacing it with a no-op; the UXG connector replaces it


                    vms = [v for v in node.get("vms",[]) if v["name"] not in rendered_vms]
                    if vms:
                        vm_tw = len(vms)*VM_W + (len(vms)-1)*VM_HGAP
                        vm_x0 = ccx - vm_tw // 2
                        vm_y  = card_y + DH + 16
                        row_has_vms = True
                        conn_lines.append(
                            f'<line x1="{ccx}" y1="{card_y+DH}" '
                            f'x2="{ccx}" y2="{vm_y}" '
                            f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="2,2"/>')
                        for k, vm in enumerate(vms):
                            vx  = vm_x0 + k*(VM_W+VM_HGAP)
                            vcx = vx + VM_W//2
                            conn_lines.append(
                                f'<line x1="{ccx}" y1="{vm_y}" '
                                f'x2="{vcx}" y2="{vm_y}" '
                                f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="2,2"/>')
                            render_vm_card(vx, vm_y, vm)
                            rendered_vms.add(vm["name"])

                else:  # cluster box
                    ms   = it["nodes"]
                    cid  = it["cid"]
                    primary_node = next((n for n in ms if n.get("cluster_primary")), ms[0])
                    cn   = primary_node.get("cluster_name", f"cluster-{cid}")
                    cdesc = primary_node.get("cluster_desc", "")
                    bw   = iw
                    # Box header height: name line + optional desc line
                    hdr_h = 13 + (11 if cdesc else 0)
                    bh   = CLUSTER_PAD + hdr_h + 6 + DH + CLUSTER_PAD
                    bx   = icx - bw // 2

                    # Cluster box — green border
                    a(f'<rect x="{bx}" y="{box_top_y}" width="{bw}" height="{bh}" rx="3" '
                      f'fill="none" stroke="{C["lab"]}" stroke-width="1.5" '
                      f'stroke-dasharray="4,3"/>')
                    # Cluster name — bold, head colour
                    a(f'<text x="{icx}" y="{box_top_y + CLUSTER_PAD + 10}" '
                      f'text-anchor="middle" fill="{C["head"]}" '
                      f'font-size="8" font-weight="600" letter-spacing="0.5">'
                      f'{_xml(cn)}</text>')
                    # Cluster description — dim, smaller
                    if cdesc:
                        a(f'<text x="{icx}" y="{box_top_y + CLUSTER_PAD + 21}" '
                          f'text-anchor="middle" fill="{C["dim"]}" font-size="6.5">'
                          f'{_xml(cdesc[:40])}</text>')

                    total_inner = len(ms)*DW + (len(ms)-1)*HGAP
                    xi = icx - total_inner//2
                    inner_xs = []
                    for _ in ms:
                        inner_xs.append(xi)
                        xi += DW + HGAP

                    # Cards sit below the cluster header
                    inner_card_y = box_top_y + CLUSTER_PAD + hdr_h + 6
                    for j2, node in enumerate(ms):
                        bdr2 = C["lab"] if node.get("status","active") == "active" else C["off"]
                        ccx, ccy = render_card(inner_xs[j2], inner_card_y,
                                               node["name"], node.get("description",""), bdr2)
                        pos_index[node["name"]] = (ccx, ccy)

                    primary = next((n for n in ms if n.get("cluster_primary")), ms[0])
                    vms = [v for v in primary.get("vms",[]) if v["name"] not in rendered_vms]
                    box_bottom = box_top_y + bh
                    if vms:
                        vm_tw = len(vms)*VM_W + (len(vms)-1)*VM_HGAP
                        vm_x0 = icx - vm_tw//2
                        vm_y  = box_bottom + 16
                        row_has_vms = True
                        # Each VM gets its own line from vm-top-center → box-bottom-center
                        for k, vm in enumerate(vms):
                            vx  = vm_x0 + k*(VM_W+VM_HGAP)
                            vcx = vx + VM_W//2
                            conn_lines.append(
                                f'<line x1="{vcx}" y1="{vm_y}" '
                                f'x2="{icx}" y2="{box_bottom}" '
                                f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="2,2"/>')
                            render_vm_card(vx, vm_y, vm)
                            rendered_vms.add(vm["name"])

            row_y += CLUSTER_PAD*2 + 14 + DH + (VM_H + 24 if row_has_vms else 0) + ROW_GAP

    # ── Physical cables ────────────────────────────────────────────────────────
    for (na, nb_) in cables:
        if na in pos_index and nb_ in pos_index:
            ax, ay = pos_index[na]
            bx2, by2 = pos_index[nb_]
            # Edge-to-edge: upper card bottom → lower card top
            if ay < by2:
                ay_e, by2_e = ay + DH//2, by2 - DH//2
            elif ay > by2:
                ay_e, by2_e = ay - DH//2, by2 + DH//2
            else:
                ay_e, by2_e = ay, by2   # same row, side connection
            conn_lines.append(
                f'<line x1="{ax}" y1="{ay_e}" x2="{bx2}" y2="{by2_e}" '
                f'stroke="{C["cable"]}" stroke-width="2" opacity="0.85"/>')

    # ── VPN tunnels ────────────────────────────────────────────────────────────
    tunnel_elems: list[str] = []
    for (na, nb_, label, style, tdesc) in tunnels:
        if na in pos_index and nb_ in pos_index:
            ax, ay   = pos_index[na]
            bx2, by2 = pos_index[nb_]
            color = C["acc"] if style == "wireguard" else C["dim"]
            dash  = "6,4"    if style == "wireguard" else "3,4"
            if ay < by2:
                ay_edge  = ay  + DH // 2
                by2_edge = by2 - DH // 2
            else:
                ay_edge  = ay  - DH // 2
                by2_edge = by2 + DH // 2
            mx, my = (ax+bx2)//2, (ay_edge+by2_edge)//2
            tunnel_elems.append(
                f'<line x1="{ax}" y1="{ay_edge}" x2="{bx2}" y2="{by2_edge}" '
                f'stroke="{color}" stroke-width="2.5" '
                f'stroke-dasharray="{dash}" opacity="1"/>')
            lk = "[WG] " if style == "wireguard" else ""
            label_text = f"{lk}{label}"
            # Estimate text width (approx 6px per char at font-size 7)
            label_w = len(label_text) * 6 + 16
            desc_w  = len(tdesc) * 5 + 16 if tdesc else 0
            badge_w = max(84, label_w, desc_w)
            badge_h = 27 if tdesc else 16
            tunnel_elems.append(
                f'<rect x="{mx - badge_w//2}" y="{my - badge_h//2}" '
                f'width="{badge_w}" height="{badge_h}" rx="2" '
                f'fill="{C["bg"]}" stroke="{color}" stroke-width="1"/>')
            tunnel_elems.append(
                f'<text x="{mx}" y="{my - badge_h//2 + 11}" text-anchor="middle" '
                f'fill="{color}" font-size="7" font-weight="600" letter-spacing="0.5">'
                f'{_xml(label_text)}</text>')
            if tdesc:
                tunnel_elems.append(
                    f'<text x="{mx}" y="{my - badge_h//2 + 22}" text-anchor="middle" '
                    f'fill="{C["dim"]}" font-size="6">'
                    f'{_xml(tdesc[:50])}</text>')

    # ── Final SVG assembly (painter's model — order = z-order) ────────────────
    # Rebuild svg with correct layer order:
    # 1. background rect (already svg[0] and svg[1])
    # 2. inet_elems  — Internet band rect sits BELOW the tunnel line
    # 3. conn_lines  — cables and VM connectors
    # 4. zone boxes + cards (already appended to svg above)
    # 5. tunnel_elems — WireGuard on top of everything
    final_svg: list[str] = svg[:2]           # <svg> opening + background rect
    final_svg += inet_elems                  # Internet band (below all lines)
    final_svg += conn_lines                  # cables / VM connectors
    final_svg += svg[2:]                     # zone boxes + cards already rendered
    final_svg += tunnel_elems               # WireGuard tunnel always on top
    final_svg.append('</svg>')
    return "\n".join(final_svg)



# ══════════════════════════════════════════════════════════════════════════════
#  Cluster diagram builder
# ══════════════════════════════════════════════════════════════════════════════

def build_cluster_svg(
    cluster_name: str,
    cluster_desc: str,
    nodes:        list[dict],   # cluster member devices
    vms:          list[dict],   # VMs belonging to this cluster
    context_nodes: list[dict],  # switches/parents the nodes connect to
    cables:       list[tuple[str,str]],
) -> str:
    """
    Dedicated diagram for a single cluster.
    Layout:
      Row 0: context nodes (ToR switches etc.)
      Row 1: cluster member devices (inside a cluster box)
      Row 2: VMs
    """
    W, PAD = 960, 32
    ZW = W - 2 * PAD

    # Card sizes — slightly larger for readability in dedicated view
    CW, CH   = 190, 80
    VMW, VMH = 160, 50
    GAP      = 18

    svg: list[str] = []
    a = svg.append
    conn_lines: list[str] = []
    pos_index: dict[str, tuple[int,int]] = {}

    def _xs(count: int, zone_x: int, zone_w: int, cw: int, gap: int) -> list[int]:
        total = count * cw + max(0, count-1) * gap
        x0 = zone_x + max(0, (zone_w - total) // 2)
        return [x0 + i*(cw+gap) for i in range(count)]

    def _card(x, y, name, desc, bdr, cw=CW, ch=CH):
        ccx, ccy = x + cw//2, y + ch//2
        a(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="2" '
          f'fill="{C["bg"]}" stroke="{bdr}" stroke-width="1"/>')
        nls = _wrap(name, 22)[:2]
        dls = _desc_lines(desc, 32)
        nh  = len(nls) * 13
        th  = nh + (len(dls)*11 + 4 if dls else 0)
        sy  = y + max(10, (ch-th)//2 + 11)
        for j, ln in enumerate(nls):
            a(f'<text x="{ccx}" y="{sy+j*13}" text-anchor="middle" '
              f'fill="{C["head"]}" font-size="10" font-weight="600">{ln}</text>')
        dy2 = sy + nh + 4
        for j, dl in enumerate(dls):
            if dy2 + j*11 < y + ch - 4:
                a(f'<text x="{ccx}" y="{dy2+j*11}" text-anchor="middle" '
                  f'fill="{C["dim"]}" font-size="7">{dl}</text>')
        return ccx, ccy

    def _vm_card(x, y, vm):
        ccx = x + VMW//2
        status = vm.get("status", "active")
        bdr    = C["lab"] if status == "active" else C["off"]
        a(f'<rect x="{x}" y="{y}" width="{VMW}" height="{VMH}" rx="2" '
          f'fill="{C["vm_bg"]}" stroke="{bdr}" stroke-width="1" stroke-dasharray="2,2"/>')
        nls = _wrap(vm["name"], 20)[:2]
        dls = _desc_lines(vm.get("description",""), 24)
        nh  = len(nls)*12
        th  = nh + (len(dls)*10+3 if dls else 0)
        sy  = y + max(8, (VMH-th)//2 + 10)
        for j, ln in enumerate(nls):
            a(f'<text x="{ccx}" y="{sy+j*12}" text-anchor="middle" '
              f'fill="{C["head"]}" font-size="9" font-weight="600">{ln}</text>')
        dy2 = sy + nh + 3
        for j, dl in enumerate(dls):
            a(f'<text x="{ccx}" y="{dy2+j*10}" text-anchor="middle" '
              f'fill="{C["dim"]}" font-size="7">{dl}</text>')
        return ccx, y + VMH//2

    # ── Layout ────────────────────────────────────────────────────────────────
    VGAP2 = 40

    ctx_y   = PAD
    ctx_h   = CH if context_nodes else 0

    # Cluster box — width fits snugly around the node cards
    n_nodes  = len(nodes)
    box_pad  = 16
    box_lbl  = 28   # label + desc area at top of box
    # Natural width: fit all cards with padding; minimum = single card + padding
    box_w_natural = n_nodes * CW + max(0, n_nodes - 1) * GAP + box_pad * 2
    # Centre in canvas, never narrower than natural width
    box_w    = max(box_w_natural, min(ZW, box_w_natural + 60))
    box_x    = (W - box_w) // 2
    box_h    = box_lbl + box_pad + CH + box_pad
    box_y    = ctx_y + ctx_h + (VGAP2 if context_nodes else 0)

    vm_y     = box_y + box_h + VGAP2
    vm_h     = VMH if vms else 0

    H = vm_y + vm_h + PAD + 8

    a(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
      f'font-family="IBM Plex Mono, monospace">')
    a(f'<rect width="{W}" height="{H}" fill="{C["bg"]}"/>')

    # Context nodes (ToR switches etc.)
    if context_nodes:
        ctx_xs = _xs(len(context_nodes), PAD, ZW, CW, GAP)
        for j, cn in enumerate(context_nodes):
            bdr = C["pub"] if cn.get("source") == "vm" else C["lab"]
            ccx, ccy = _card(ctx_xs[j], ctx_y, cn["name"],
                              cn.get("description",""), bdr)
            pos_index[cn["name"]] = (ccx, ccy)

    # Cluster box
    box_cx = box_x + box_w // 2
    a(f'<rect x="{box_x}" y="{box_y}" width="{box_w}" height="{box_h}" rx="3" '
      f'fill="none" stroke="{C["lab"]}" stroke-width="1.5" stroke-dasharray="4,3"/>')
    a(f'<text x="{box_cx}" y="{box_y + 18}" text-anchor="middle" '
      f'fill="{C["head"]}" font-size="10" font-weight="600" letter-spacing="0.5">'
      f'{_xml(cluster_name)}</text>')
    if cluster_desc:
        a(f'<text x="{box_cx}" y="{box_y + 30}" text-anchor="middle" '
          f'fill="{C["dim"]}" font-size="7">{_xml(cluster_desc)}</text>')

    # Cluster member cards
    node_xs = _xs(n_nodes, box_x + box_pad, box_w - box_pad * 2, CW, GAP)
    node_y  = box_y + box_lbl + box_pad

    # Build parent→children map so we draw one line per UNIQUE parent
    parent_to_nodes: dict[str, list[tuple[int,int]]] = {}
    for j, node in enumerate(nodes):
        pname = node.get("_parent")
        if not pname:
            pname = context_nodes[0]["name"] if context_nodes else None
        if pname:
            parent_to_nodes.setdefault(pname, [])

    for j, node in enumerate(nodes):
        bdr = C["lab"] if node.get("status","active") == "active" else C["off"]
        ccx, ccy = _card(node_xs[j], node_y, node["name"],
                         node.get("description",""), bdr)
        pos_index[node["name"]] = (ccx, ccy)
        pname = node.get("_parent")
        if not pname and context_nodes:
            pname = context_nodes[0]["name"]
        if pname:
            parent_to_nodes.setdefault(pname, []).append((ccx, node_y))

    # Draw one line per node from its parent's bottom edge to the node's top edge
    for pname, child_positions in parent_to_nodes.items():
        if pname not in pos_index:
            continue
        px, py = pos_index[pname]
        for ccx, cy_top in child_positions:
            conn_lines.append(
                f'<line x1="{px}" y1="{py + CH//2}" '
                f'x2="{ccx}" y2="{cy_top}" '
                f'stroke="{C["cable"]}" stroke-width="2" opacity="0.85"/>')

    # VMs centred under cluster box
    if vms:
        box_cx  = box_x + box_w//2
        box_bot = box_y + box_h
        vm_xs   = _xs(len(vms), PAD, ZW, VMW, GAP)
        conn_lines.append(
            f'<line x1="{box_cx}" y1="{box_bot}" x2="{box_cx}" y2="{vm_y}" '
            f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="2,2"/>')
        for k, vm in enumerate(vms):
            vcx = vm_xs[k] + VMW//2
            conn_lines.append(
                f'<line x1="{box_cx}" y1="{vm_y}" '
                f'x2="{vcx}" y2="{vm_y}" '
                f'stroke="{C["border"]}" stroke-width="1" stroke-dasharray="2,2"/>')
            _vm_card(vm_xs[k], vm_y, vm)

    final: list[str] = svg[:2] + conn_lines + svg[2:]
    final.append('</svg>')
    return "\n".join(final)


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate network diagram(s) from Netbox"
    )
    parser.add_argument(
        "--cluster-diagrams", action="store_true",
        help="Generate a separate SVG/PNG for each Proxmox/virtualisation cluster"
    )
    parser.add_argument(
        "--cluster", metavar="NAME",
        help="Generate diagram only for this cluster name (implies --cluster-diagrams)"
    )
    parser.add_argument(
        "--no-simplify", action="store_true",
        help="In the main diagram, expand cluster boxes to show individual nodes "
             "instead of a grouped box (default: simplified/boxed)"
    )
    parser.add_argument(
        "--main-diagram", action="store_true", default=True,
        help="Generate the main overview diagram (default: on)"
    )
    parser.add_argument(
        "--no-main-diagram", action="store_false", dest="main_diagram",
        help="Skip the main overview diagram"
    )
    args = parser.parse_args()

    if args.cluster:
        args.cluster_diagrams = True

    pub, lab, cables, tunnels, raw_count = fetch_all()
    now    = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    source = "netbox"

    if not pub and not lab:
        print("[WARN] No tagged devices – using static fallback.")
        pub, lab, cables = FALLBACK_PUBLIC, FALLBACK_HOMELAB, []
        tunnels = LOGICAL_TUNNELS_FALLBACK
        source  = "static"

    total_vms = sum(len(n.get("vms", [])) for n in lab)
    os.makedirs("assets", exist_ok=True)

    # ── Main overview diagram ──────────────────────────────────────────────────
    cluster_meta: list[dict] = []

    if args.main_diagram:
        svg_str = build_svg(SAAS_NODES, pub, lab, cables, tunnels,
                            simplify=not args.no_simplify)
        with open(OUTPUT_SVG, "w", encoding="utf-8") as f:
            f.write(svg_str)
        print(f"[OK] {OUTPUT_SVG}  saas={len(SAAS_NODES)}  pub={len(pub)}  "
              f"lab={len(lab)}  vms={total_vms}  cables={len(cables)}")
        try:
            import cairosvg
            cairosvg.svg2png(url=OUTPUT_SVG, write_to=OUTPUT_PNG, output_width=1920)
            print(f"[OK] {OUTPUT_PNG}")
        except ImportError:
            print("[INFO] cairosvg not available – PNG skipped.")

    # ── Cluster detail diagrams ────────────────────────────────────────────────
    if args.cluster_diagrams:
        # Collect all clusters from lab nodes
        seen_clusters: dict[str, dict] = {}
        node_by_name: dict[str, dict] = {n["name"]: n for n in lab}

        for node in lab:
            cname = node.get("cluster_name", "")
            if not cname:
                continue
            if args.cluster and cname != args.cluster:
                continue
            # Skip clusters tagged diagram-cluster-exclude unless explicitly requested
            if node.get("cluster_excluded") and not args.cluster:
                print(f"[INFO] Skipping cluster '{cname}' (tagged {TAG_CLUSTER_EXCLUDE})")
                continue
            if cname not in seen_clusters:
                seen_clusters[cname] = {
                    "name":  cname,
                    "desc":  node.get("cluster_desc", ""),
                    "notes": node.get("cluster_notes", ""),
                    "nodes": [],
                    "vms":   node.get("vms", []) if node.get("cluster_primary") else [],
                }
            seen_clusters[cname]["nodes"].append(node)

        for cname, cdata in seen_clusters.items():
            # Find context nodes: direct parents of cluster members
            context_names: set[str] = set()
            for node in cdata["nodes"]:
                p = node.get("_parent")
                if p and p in node_by_name:
                    context_names.add(p)
            context_nodes = [node_by_name[n] for n in sorted(context_names)
                             if n in node_by_name]

            safe_name = cname.lower().replace(" ", "-").replace("/", "-")
            out_svg   = f"assets/diagram-cluster-{safe_name}.svg"
            out_png   = f"assets/diagram-cluster-{safe_name}.png"

            svg_str = build_cluster_svg(
                cluster_name  = cname,
                cluster_desc  = cdata["desc"],
                nodes         = sorted(cdata["nodes"], key=lambda n: n["name"]),
                vms           = cdata["vms"],
                context_nodes = context_nodes,
                cables        = cables,
            )
            with open(out_svg, "w", encoding="utf-8") as f:
                f.write(svg_str)
            print(f"[OK] {out_svg}  nodes={len(cdata['nodes'])}  "
                  f"vms={len(cdata['vms'])}  context={len(context_nodes)}")

            try:
                import cairosvg
                cairosvg.svg2png(url=out_svg, write_to=out_png, output_width=1920)
                print(f"[OK] {out_png}")
            except ImportError:
                pass

            cluster_meta.append({
                "name":         cname,
                "description":  cdata["desc"],
                "notes":        cdata.get("notes", ""),
                "node_count":   len(cdata["nodes"]),
                "nodes":        [n["name"] for n in cdata["nodes"]],
                "vm_count":     len(cdata["vms"]),
                "svg":          out_svg,
                "png":          out_png,
            })

    # ── Meta JSON ──────────────────────────────────────────────────────────────
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
        "clusters":       cluster_meta,
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
