// Detailansicht: was auf der Seite stand, wie sie eingeordnet wurde, was sich tat.
export default function DomainDetail({ data, onClose }) {
  if (!data) return null;

  const { changes = [], ...d } = data;

  return (
    <div className="overlay" onClick={onClose}>
      <div className="detail" onClick={(e) => e.stopPropagation()}>
        <header className="detail-head">
          <div>
            <h2>{d.display_name || d.domain}</h2>
            <p className="sub">
              {d.title || "kein Titel"}
              {d.final_url ? ` · ${d.final_url}` : ""}
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="Schließen">
            ×
          </button>
        </header>

        <div className="grid">
          <Field label="Zuerst gesehen" value={fmt(d.first_seen)} />
          <Field label="Kategorie" value={d.category || "–"} />
          <Field label="Segment" value={d.segment || "–"} />
          <Field label="Geschäftsart" value={d.business_type || "–"} />
          <Field
            label="Eigenes Produkt"
            value={d.sells_own_product ? "ja" : "nein"}
          />
          <Field
            label="Konfidenz"
            value={d.confidence != null ? d.confidence.toFixed(2) : "–"}
          />
          <Field label="Betreiber" value={d.company_name || "–"} />
          <Field label="Rechtsform" value={d.legal_form || "–"} />
          <Field label="Sprache" value={d.lang || "–"} />
          <Field label="Textmenge" value={`${d.text_len || 0} Zeichen`} />
          <Field label="HTTP" value={d.http_status ?? "–"} />
          <Field label="Geparkt" value={d.is_parked ? "ja" : "nein"} />
        </div>

        {d.meta_description ? (
          <section>
            <h3>Beschreibung</h3>
            <p className="desc">{d.meta_description}</p>
          </section>
        ) : null}

        {d.summary_de ? (
          <section>
            <h3>Einordnung</h3>
            <p className="desc">{d.summary_de}</p>
          </section>
        ) : null}

        {changes.length ? (
          <section>
            <h3>Änderungen</h3>
            <ul className="changes">
              {changes.map((c, i) => (
                <li key={i}>
                  <span className="kind">{c.kind}</span> {fmt(c.detected_at)}
                  {c.detail ? <em> {c.detail}</em> : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        <footer className="detail-foot">
          Quelle: öffentliche Certificate-Transparency-Logs. "Zuerst gesehen" ist
          der Zeitpunkt des ersten Zertifikats, nicht das Registrierungsdatum.
        </footer>
      </div>
    </div>
  );
}

function Field({ label, value }) {
  return (
    <div className="field">
      <span className="label">{label}</span>
      <span className="value">{value}</span>
    </div>
  );
}

function fmt(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("de-DE");
}