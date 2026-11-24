import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import DomainTable from "./DomainTable.jsx";
import DomainDetail from "./DomainDetail.jsx";
import Settings from "./Settings.jsx";

export default function App() {
  const [page, setPage] = useState("radar");
  const [view, setView] = useState("market");
  const [rows, setRows] = useState([]);
  const [stats, setStats] = useState(null);
  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [sort, setSort] = useState("newest");
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    api
      .stats()
      .then(setStats)
      .catch((e) => setError(e.message));
    api
      .categories()
      .then((r) => setCategories(r.hits || []))
      .catch(() => {});
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.startups({
        view,
        q: q || undefined,
        category: category || undefined,
        sort,
        limit: 200,
      });
      setRows(res.hits || []);
    } catch (e) {
      setError(e.message);
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [view, q, category, sort]);

  // Debounce, damit Tippen keine Abfragekaskade auslöst.
  useEffect(() => {
    const t = setTimeout(load, 250);
    return () => clearTimeout(t);
  }, [load]);

  const openDomain = useCallback(async (domain) => {
    setDetail({ domain });
    try {
      const res = await api.domain(domain);
      setDetail(res);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const totals = useMemo(() => {
    if (!stats) return null;
    return stats;
  }, [stats]);

  return (
    <div className="app">
      <header className="head">
        <div>
          <h1>DomainRadar</h1>
          <p className="sub">
            Neu beobachtete .de-Domains aus Certificate-Transparency-Logs.
            Vollständig lokal.
          </p>
        </div>

        <nav className="views" aria-label="Bereich">
          <button
            className={page === "radar" ? "tab active" : "tab"}
            onClick={() => setPage("radar")}
            aria-current={page === "radar"}
          >
            Radar
          </button>
          <button
            className={page === "settings" ? "tab active" : "tab"}
            onClick={() => setPage("settings")}
            aria-current={page === "settings"}
          >
            Einstellungen
          </button>
        </nav>
      </header>

      {page === "settings" ? (
        <Settings />
      ) : (
        <>
          <div className="views views-sub" role="tablist" aria-label="Ansicht">
            <button
              className={view === "market" ? "tab active" : "tab"}
              onClick={() => setView("market")}
              role="tab"
              aria-selected={view === "market"}
            >
              Markt
            </button>
            <button
              className={view === "leads" ? "tab active" : "tab"}
              onClick={() => setView("leads")}
              role="tab"
              aria-selected={view === "leads"}
            >
              Leads
            </button>
          </div>

          {totals ? (
            <section className="cards">
              <Card label="Domains gesamt" value={fmtNum(totals.total_domains)} />
              <Card label="neu (24 h)" value={fmtNum(totals.new_24h)} />
              <Card label="neu (7 Tage)" value={fmtNum(totals.new_7d)} />
              <Card label="eingeordnet" value={fmtNum(totals.classified)} />
              <Card label="mit Produkt" value={fmtNum(totals.with_product)} />
              <Card label="geparkt" value={fmtNum(totals.parked)} />
            </section>
          ) : null}

          <section className="filters">
            <input
              type="search"
              placeholder="Domain oder Titel suchen …"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              className="search"
            />
            <select value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">Alle Kategorien</option>
              {categories.map((c) => (
                <option key={c.category} value={c.category}>
                  {c.category} ({c.count})
                </option>
              ))}
            </select>
            <select value={sort} onChange={(e) => setSort(e.target.value)}>
              <option value="newest">Neueste zuerst</option>
              <option value="oldest">Älteste zuerst</option>
              <option value="confidence">Konfidenz</option>
            </select>
            <span className="count">
              {loading ? "lädt …" : `${rows.length} Treffer`}
            </span>
          </section>

          {error ? <p className="error">Fehler: {error}</p> : null}

          <main>
            <DomainTable rows={rows} view={view} onOpen={openDomain} />
          </main>

          {detail ? (
            <DomainDetail data={detail} onClose={() => setDetail(null)} />
          ) : null}
        </>
      )}

      <footer className="foot">
        Quelle: öffentliche Certificate-Transparency-Logs. „Zuerst gesehen“ =
        erstes Zertifikat, nicht Registrierungsdatum. Geparkte Domains ohne
        HTTPS fehlen.
      </footer>
    </div>
  );
}

function Card({ label, value }) {
  return (
    <div className="card">
      <span className="card-value">{value}</span>
      <span className="card-label">{label}</span>
    </div>
  );
}

function fmtNum(n) {
  if (n == null) return "–";
  return n.toLocaleString("de-DE");
}