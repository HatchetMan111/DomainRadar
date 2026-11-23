// Zwei Sichten auf denselben Bestand: Markt (breit) und Leads (Produkt + Betreiber).
// Der Umschalter ändert nur den Filter, nicht die Datenquelle.

export default function DomainTable({ rows, view, onOpen }) {
  if (!rows.length) {
    return <p className="empty">Keine Treffer.</p>;
  }

  return (
    <table className="table">
      <thead>
        <tr>
          <th>Domain</th>
          <th>Beobachtet</th>
          {view === "leads" ? (
            <>
              <th>Betreiber</th>
              <th>Rechtsform</th>
            </>
          ) : null}
          <th>Kategorie</th>
          <th>Segment</th>
          <th>Produkt</th>
          <th>Konfidenz</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.domain} onClick={() => onOpen(r.domain)}>
            <td className="domain" title={r.domain}>{r.display_name || r.domain}</td>
            <td>{fmtDate(r.first_seen)}</td>
            {view === "leads" ? (
              <>
                <td>{r.company_name || "–"}</td>
                <td>{r.legal_form || "–"}</td>
              </>
            ) : null}
            <td>{r.category || "–"}</td>
            <td>{r.segment || "–"}</td>
            <td>{r.sells_own_product ? "ja" : "nein"}</td>
            <td>{r.confidence != null ? r.confidence.toFixed(2) : "–"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function fmtDate(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}