#!/usr/bin/env python3
"""
generate_diagram.py
====================
Fetches devices, cables and virtual circuits from Netbox Cloud Free API
and generates a network diagram as SVG (+ optional PNG via cairosvg).

──────────────────────────────────────────────────────────────────────────────
FILTERING LOGIC (pure tag-based, no heuristics)
──────────────────────────────────────────────────────────────────────────────
Only devices that carry at least ONE of the following tags are shown:

  Tag slug            → Zone in diagram
  ─────────────────────────────────────
  diagram-public      → Public Infrastructure  (orange zone)
  diagram-homelab     → Home Lab               (green zone)

Everything else is silently ignored regardless of tenant, role or site.
Both tags can be combined on one device (e.g. a VPN gateway that bridges both
zones) – in that case the device appears in BOTH zones.

Tag "diagram-exclude" can be added as an explicit safety-net to suppress a
device even if it carries one of the zone tags (exclude wins).

──────────────────────────────────────────────────────────────────────────────
CONNECTION LOGIC
──────────────────────────────────────────────────────────────────────────────
Physical cables (/dcim/cables/) are fetched and drawn as solid lines between
diagram nodes when BOTH endpoints are tagged devices in the diagram.

WireGuard / logical tunnels are not in Netbox cables; they are declared in the
LOGICAL_TUNNELS list below so they appear as dashed lines with a lock icon.
Add more entries there as needed.

──────────────────────────────────────────────────────────────────────────────
ENVIRONMENT VARIABLES (GitHub Actions Secrets)
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
