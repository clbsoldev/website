# Collaboration Solution Development – GitHub Pages

Private Lab Documentation Site for `https://github.com/clbsoldev`

## Repository Structure

```
.
├── index.html                          # Main GitHub Pages site
├── assets/
│   ├── diagram.svg                     # Auto-generated network diagram (Netbox)
│   ├── diagram.png                     # Optional PNG export
│   └── diagram_meta.json              # Generation timestamp + source info
├── scripts/
│   └── generate_diagram.py            # Netbox API → SVG/PNG generator
└── .github/
    └── workflows/
        └── netbox-diagram.yml         # Scheduled GitHub Actions workflow
```

## GitHub Pages Setup

1. Go to **Settings → Pages** in this repository
2. Source: **Deploy from a branch** → `main` → `/ (root)`
3. Save – the site will be live at `https://clbsoldev.github.io/<repo-name>/`

## Netbox Diagram – GitHub Secrets

Add the following secrets under **Settings → Secrets and variables → Actions**:

| Secret          | Description                                    |
|-----------------|------------------------------------------------|
| `NETBOX_URL`    | Your Netbox Cloud Free URL, e.g. `https://yourinstance.netboxcloud.com` |
| `NETBOX_TOKEN`  | A read-only API token from Netbox              |

The workflow runs **every Sunday at 03:00 UTC** and can also be triggered manually
via **Actions → Netbox → Network Diagram → Run workflow**.

## Netbox Device Zone Assignment (pure Tag-based)

Only devices with **at least one diagram tag** appear in the diagram. Everything else is ignored.

| Tag slug | Zone |
|----------|------|
| `diagram-public` | Public Infrastructure (orange) |
| `diagram-homelab` | Home Lab (green) |
| `diagram-exclude` | Always excluded (safety override) |

A device can carry **both** `diagram-public` and `diagram-homelab` to appear in both zones (e.g. a VPN gateway).

### Netbox Tag Setup

Create once under **Extras → Tags** in Netbox:

```
diagram-public
diagram-homelab
diagram-exclude
```

Then assign the relevant tags to each device. No tenant, site or role filtering is applied – tags are the only selector.

### Cables & Connections

Physical connections are fetched from `/dcim/cables/`. A cable appears as a solid line in the diagram if **both** endpoint devices are tagged.

**WireGuard and other logical tunnels** are not stored as Netbox cables. Add them to the `LOGICAL_TUNNELS` list in `scripts/generate_diagram.py`:

```python
LOGICAL_TUNNELS = [
    ("VPS nginx", "Unifi Gateway", "WireGuard VPN", "wireguard"),
    # ("Device A", "Device B", "Label", "logical"),
]
```

Device names must match the Netbox device names exactly.

## Manual Diagram Regeneration

```bash
export NETBOX_URL=https://yourinstance.netboxcloud.com
export NETBOX_TOKEN=your_token_here
python scripts/generate_diagram.py
```

## Impressum

Update the placeholder fields in `index.html` (search for `[Vorname Nachname]`, 
`[Straße Hausnummer]`, `[PLZ Ort]`, `mail@example.com`) before publishing.
