/**
 * clusters.js
 * Loads assets/diagram_meta.json and renders cluster sections.
 * Data source: static JSON committed to repo by GitHub Actions — no live API calls.
 */

(function () {
  const grid    = document.getElementById('cluster-grid');
  const metaLine = document.getElementById('meta-line');

  fetch('assets/diagram_meta.json')
    .then(r => r.ok ? r.json() : Promise.reject('not found'))
    .then(meta => {
      metaLine.innerHTML =
        `Last generated: <span id="meta-ts">${meta.generated_at || '—'}</span> &nbsp;·&nbsp; ` +
        `Source: ${meta.source || '—'} &nbsp;·&nbsp; ` +
        `${meta.lab_nodes || 0} lab devices &nbsp;·&nbsp; ` +
        `${meta.vm_count  || 0} VMs`;

      const clusters = (meta.clusters || [])
        .slice()
        .sort((a, b) => a.name.localeCompare(b.name));

      if (clusters.length === 0) {
        grid.innerHTML = `
          <div class="generate-hint" style="border-left-color:var(--border)">
            No cluster diagrams have been generated yet.
            Run the GitHub Action manually with <code>cluster_diagrams = true</code>,
            or run <code>python scripts/generate_diagram.py --cluster-diagrams</code> locally.
          </div>`;
        return;
      }

      clusters.forEach(cl => {
        const section = document.createElement('div');
        section.className = 'cluster-section';

        const badgesHtml = [
          `<span class="badge hi">${cl.node_count} node${cl.node_count !== 1 ? 's' : ''}</span>`,
          `<span class="badge">${cl.vm_count} VM${cl.vm_count !== 1 ? 's' : ''}</span>`,
        ].join('');

        const nodesHtml = (cl.nodes || [])
          .map(n => `<span class="node-pill">${n}</span>`)
          .join('');

        const notesHtml = cl.notes
          ? `<div class="cluster-notes">${cl.notes}</div>`
          : '';

        // Cache-bust so updated SVGs are always shown
        const svgUrl = cl.svg + '?' + Date.now();

        section.innerHTML = `
          <div class="cluster-header">
            <div>
              <h2 class="cluster-title">${cl.name}</h2>
              ${cl.description ? `<div class="cluster-desc">${cl.description}</div>` : ''}
              ${notesHtml}
            </div>
            <div class="cluster-badges">${badgesHtml}</div>
          </div>
          <div class="cluster-diagram">
            <img src="${svgUrl}" alt="Cluster diagram: ${cl.name}"
                 onerror="this.style.display='none';
                          this.nextElementSibling.style.display='block'" />
            <div class="no-diagram" style="display:none">
              Diagram not yet generated — run the Action with
              <code>--cluster ${cl.name}</code>
            </div>
          </div>`;

        grid.appendChild(section);
      });
    })
    .catch(() => {
      grid.innerHTML = `
        <p class="text-dim">
          Could not load metadata — have the diagrams been generated yet?
        </p>`;
      metaLine.textContent = 'Metadata unavailable.';
    });
}());
