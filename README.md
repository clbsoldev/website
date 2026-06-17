# Collaboration Solution Development · Lab Website

Static GitHub Pages site documenting the **clbsoldev** home lab —
a private environment for developing, evaluating and documenting
Cisco Collaboration & Webex solutions.

🌐 **Live:** `https://clb-sol.dev`
📊 **Status:** `https://status.clb-sol.dev`
🐙 **GitHub Org:** `https://github.com/clbsoldev`

---

## Pages

| Page | Description |
|------|-------------|
| `index.html` | Landing page: hero, about / purpose cards |
| `network.html` | Network architecture, WireGuard tunnel, auto-generated diagram |
| `clusters.html` | Virtualization cluster detail diagrams |
| `naming.html` | Host naming convention with sortable tables |

Navigation groups `network.html` and `clusters.html` under an **Infrastructure** hover dropdown.

---

## Repository Structure

```
.
├── index.html                          # Landing page
├── network.html                        # Network architecture & diagram
├── clusters.html                       # Virtualization cluster detail view
├── naming.html                         # Host naming convention
├── assets/
│   ├── style.css                       # Shared stylesheet (all pages)
│   ├── components.js                   # Web Components: <site-nav>, <site-footer>, <site-impressum>
│   ├── clusters.js                     # Cluster page logic (loads diagram_meta.json)
│   ├── diagram.svg                     # Auto-generated main network diagram
│   ├── diagram.png                     # PNG export of main network diagram
│   ├── diagram_meta.json               # Diagram metadata + cluster list (committed by CI)
│   └── diagram-cluster-<name>.svg     # Per-cluster detail diagrams (generated on demand)
├── data/
│   ├── impressum.json                  # Legal contact data — edit here, not in components.js
│   ├── naming-sites.json               # Site/location prefix table
│   ├── naming-functions.json           # Host function code table
│   └── naming-roles.json               # Service role subdomain table
├── scripts/
│   └── generate_diagram.py             # Netbox API → SVG/PNG diagram generator
└── .github/
    └── workflows/
        └── netbox-diagram.yml          # Scheduled + push-triggered diagram generation
```

---

## GitHub Pages Setup

1. **Settings → Pages → Source:** Deploy from branch `main` / `/ (root)`
2. The site is available at `https://<org>.github.io/<repo>/`

---

## Diagram Generation

Diagrams are auto-generated from **Netbox Cloud Free** via GitHub Actions
and committed back to the repo as static SVG/PNG files — no live API calls from the browser.

### Triggers

| Trigger | When | What |
|---------|------|------|
| Push to `scripts/generate_diagram.py` | On every push | Main diagram + all cluster diagrams |
| Scheduled | Every Monday 18:00 UTC | Main diagram + all cluster diagrams |
| Manual (`workflow_dispatch`) | On demand | Configurable via inputs (see below) |

### Manual run inputs

**Actions → Netbox → Network Diagram → Run workflow**

| Input | Default | Description |
|-------|---------|-------------|
| `cluster_diagrams` | `false` | Generate per-cluster detail SVGs |
| `cluster` | *(empty)* | Generate diagram for one specific cluster only |

### Local run

```bash
export NETBOX_URL=https://yourinstance.netboxcloud.com
export NETBOX_TOKEN=your_readonly_token

# Main overview diagram (default)
python scripts/generate_diagram.py

# Main diagram + all cluster detail diagrams
python scripts/generate_diagram.py --cluster-diagrams

# Single cluster detail diagram only
python scripts/generate_diagram.py --cluster hau-hypcl01

# Expand cluster boxes to individual nodes (no grouping box)
python scripts/generate_diagram.py --no-simplify

# Skip main diagram, generate cluster diagrams only
python scripts/generate_diagram.py --no-main-diagram --cluster-diagrams
```

---

## Netbox Tag Reference

Create these tags under **Extras → Tags** in Netbox and assign them to devices/VMs/clusters:

### Device & VM tags

| Tag slug | Effect |
|----------|--------|
| `diagram-public` | Appears in the **Public Infrastructure** zone |
| `diagram-homelab` | Appears in the **Home Lab** zone |
| `diagram-exclude` | Always excluded from all diagrams |
| `diagram-no-vms` | Cluster VMs are not rendered as children of this device |

A device can carry both `diagram-public` and `diagram-homelab` to appear in both zones.

### Cluster tags

| Tag slug | Effect |
|----------|--------|
| `diagram-cluster-exclude` | Cluster is skipped during `--cluster-diagrams` runs (unless explicitly named with `--cluster`) |

### Custom fields (optional, on Cluster objects)

| Field name | Type | Effect |
|------------|------|--------|
| `diagram_notes` | Text | Shown as description text on the Clusters page |

If `diagram_notes` is not set, the `comments` field is used as fallback.

---

## Impressum

Edit `data/impressum.json` to update the legal contact information.
The `<site-impressum>` web component loads this file at runtime —
`assets/components.js` never needs to be edited for contact data changes.

---

## GitHub Secrets

Add under **Settings → Secrets and variables → Actions** (Repository or Organization level):

| Secret | Description |
|--------|-------------|
| `NETBOX_URL` | `https://yourinstance.netboxcloud.com` |
| `NETBOX_TOKEN` | Read-only Netbox API token (Token format, no Bearer prefix) |

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Hosting | GitHub Pages (static, no server-side rendering) |
| Styles | Vanilla CSS, CSS custom properties — `assets/style.css` |
| Components | Native Web Components, no framework, no build step — `assets/components.js` |
| Page logic | Vanilla JavaScript modules — `assets/clusters.js` |
| Diagram generation | Python 3.13 + Netbox REST API → SVG/PNG |
| Data | Static JSON in `data/`, loaded client-side via `fetch()` |
| CI/CD | GitHub Actions (`netbox-diagram.yml`) |
