/**
 * clbsoldev · Web Components
 * <site-nav active="...">  — sticky navigation with Infrastructure hover dropdown
 * <site-footer>            — shared footer
 * <site-impressum>         — impressum block (loads from data/impressum.json)
 */

// ── <site-nav> ────────────────────────────────────────────────────────────────
class SiteNav extends HTMLElement {
  connectedCallback() {
    const active = this.getAttribute('active') || '';

    this.innerHTML = `
      <nav class="site-nav">
        <a class="nav-brand" href="index.html"><span>// </span>clbsoldev</a>
        <ul class="nav-links">
          <li><a href="index.html#about"
            ${active === 'about' ? 'class="active"' : ''}>About</a></li>

          <li class="nav-dropdown">
            <a class="nav-dropdown-toggle${['network','clusters'].includes(active) ? ' active' : ''}"
               href="#">Infrastructure ▾</a>
            <ul class="nav-dropdown-menu">
              <li><a href="network.html"
                ${active === 'network'  ? 'class="active"' : ''}>Network</a></li>
              <li><a href="clusters.html"
                ${active === 'clusters' ? 'class="active"' : ''}>Virtualization</a></li>
            </ul>
          </li>

          <li><a href="naming.html"
            ${active === 'naming' ? 'class="active"' : ''}>Naming</a></li>
          <li><a href="index.html#impressum"
            ${active === 'impressum' ? 'class="active"' : ''}>Impressum</a></li>
          <li><a href="https://status.clb-sol.dev"
            target="_blank" rel="noopener">Status ↗</a></li>
          <li><a href="https://github.com/clbsoldev"
            target="_blank" rel="noopener">GitHub ↗</a></li>
        </ul>
      </nav>`;
  }
}

// ── <site-footer> ─────────────────────────────────────────────────────────────
class SiteFooter extends HTMLElement {
  connectedCallback() {
    this.innerHTML = `
      <footer class="site-footer">
        <div class="footer-left">© ${new Date().getFullYear()} clbsoldev · Collaboration Solution Development Lab</div>
        <div class="footer-right">
          <a href="https://github.com/clbsoldev" target="_blank" rel="noopener">github.com/clbsoldev</a>
          &nbsp;·&nbsp;
          <a href="index.html#impressum">Impressum</a>
        </div>
      </footer>`;
  }
}

// ── <site-impressum> ──────────────────────────────────────────────────────────
class SiteImpressum extends HTMLElement {
  connectedCallback() {
    // Load from data/impressum.json — edit that file, not this component
    fetch('data/impressum.json')
      .then(r => r.ok ? r.json() : null)
      .then(d => d || {})
      .catch(() => ({}))
      .then(d => {
        const name    = d.name    || '[Name]';
        const street  = d.street  || '[Straße]';
        const city    = d.city    || '[PLZ Ort]';
        const country = d.country || 'Deutschland';
        const email   = d.email   || 'mail@example.com';
        const github  = d.github  || 'https://github.com/clbsoldev';
        const ghLabel = github.replace('https://', '');

        this.innerHTML = `
          <div class="impressum-grid">
            <div class="imp-block">
              <h3>Verantwortlich</h3>
              <address>
                ${name}<br/>
                ${street}<br/>
                ${city}<br/>
                ${country}
              </address>
            </div>
            <div class="imp-block">
              <h3>Kontakt</h3>
              <p>
                E-Mail: <a href="mailto:${email}">${email}</a><br/>
                GitHub: <a href="${github}" target="_blank" rel="noopener">${ghLabel}</a>
              </p>
            </div>
            <div class="imp-block">
              <h3>Haftungsausschluss</h3>
              <p>
                Diese Webseite dokumentiert eine private Testumgebung. Alle Inhalte
                dienen ausschließlich zu Dokumentations- und Lernzwecken. Es besteht
                kein kommerzieller Hintergrund.
              </p>
            </div>
            <div class="imp-block">
              <h3>Datenschutz</h3>
              <p>
                Diese Seite wird über GitHub Pages gehostet. Es gelten die
                <a href="https://docs.github.com/en/site-policy/privacy-policies/github-privacy-statement"
                   target="_blank" rel="noopener">Datenschutzbestimmungen von GitHub</a>.
                Es werden keine eigenen personenbezogenen Daten erhoben oder verarbeitet.
              </p>
            </div>
          </div>
          <div class="imp-disclaimer">
            <strong style="color:var(--text-head)">Hinweis:</strong>
            Trotz sorgfältiger inhaltlicher Kontrolle übernehme ich keine Haftung für die Inhalte
            externer Links. Für den Inhalt der verlinkten Seiten sind ausschließlich deren Betreiber
            verantwortlich.
          </div>`;
      });
  }
}

customElements.define('site-nav',        SiteNav);
customElements.define('site-footer',     SiteFooter);
customElements.define('site-impressum',  SiteImpressum);
