/**
 * clbsoldev · Web Components
 * <site-nav active="naming">   — sticky navigation bar
 * <site-footer>                — shared footer
 * <site-impressum>             — impressum block (legal)
 */

// ── <site-nav> ────────────────────────────────────────────────────────────────
class SiteNav extends HTMLElement {
  connectedCallback() {
    const active = this.getAttribute('active') || '';
    const links = [
      { href: 'index.html#about',   label: 'About',      key: 'about'     },
      { href: 'index.html#network', label: 'Network',    key: 'network'   },
      { href: 'naming.html',        label: 'Naming',     key: 'naming'    },
      { href: 'index.html#impressum', label: 'Impressum', key: 'impressum' },
      { href: 'https://status.clb-sol.dev', label: 'Status ↗', key: 'status', external: true },
      { href: 'https://github.com/clbsoldev', label: 'GitHub ↗', key: 'github', external: true },
    ];

    this.innerHTML = `
      <nav class="site-nav">
        <a class="nav-brand" href="index.html"><span>// </span>clbsoldev</a>
        <ul class="nav-links">
          ${links.map(l => `
            <li><a href="${l.href}"
              ${l.external ? 'target="_blank" rel="noopener"' : ''}
              ${l.key === active ? 'class="active"' : ''}
            >${l.label}</a></li>
          `).join('')}
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
    this.innerHTML = `
      <div class="impressum-grid">
        <div class="imp-block">
          <h3>Verantwortlich</h3>
          <address>
            Florian Strunk<br/>
            Luetzelbuchener Str. 28 b<br/>
            63454 Hanau<br/>
            Deutschland
          </address>
        </div>
        <div class="imp-block">
          <h3>Kontakt</h3>
          <p>
            E-Mail: <a href="mailto:strfl89@clb-sol.dev">strfl89@clb-sol.dev</a><br/>
            GitHub: <a href="https://github.com/clbsoldev" target="_blank" rel="noopener">github.com/clbsoldev</a>
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
  }
}

customElements.define('site-nav',        SiteNav);
customElements.define('site-footer',     SiteFooter);
customElements.define('site-impressum',  SiteImpressum);
