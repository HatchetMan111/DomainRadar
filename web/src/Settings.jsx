import { useEffect, useState } from "react";

// Einstellungsseite. Bewusst ohne Passwort-Feld-Verhalten: der API-Key wird
// maskiert angezeigt und muss zum Ändern neu eingegeben werden. So landet er
// nicht im Klartext im DOM oder im Screenshot.
export default function Settings({ onSaved }) {
  const [data, setData] = useState(null);
  const [draft, setDraft] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);
  const [testing, setTesting] = useState("");
  const [testResult, setTestResult] = useState({});

  const load = async () => {
    setLoading(true);
    try {
      const resp = await fetch("/api/settings");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const body = await resp.json();
      setData(body);
      // Nur nicht-geheime Felder vorbelegen; Geheimnisse bleiben leer.
      const next = {};
      for (const [k, v] of Object.entries(body.values)) {
        if (v && typeof v === "object" && "is_set" in v) next[k] = "";
        else next[k] = v;
      }
      setDraft(next);
    } catch (e) {
      setMsg({ kind: "error", text: e.message });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const resp = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: draft }),
      });
      const body = await resp.json();
      if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
      setMsg({
        kind: "ok",
        text:
          body.changed && body.changed.length
            ? `Gespeichert: ${body.changed.length} Werte geändert.`
            : "Gespeichert – keine Änderung.",
      });
      // Geheimnisse nach dem Speichern leeren, damit sie nicht stehen bleiben
      const cleared = { ...draft };
      for (const [k, v] of Object.entries(body.values)) {
        if (v && typeof v === "object" && "is_set" in v) cleared[k] = "";
      }
      setDraft(cleared);
      if (onSaved) onSaved();
    } catch (e) {
      setMsg({ kind: "error", text: e.message });
    } finally {
      setSaving(false);
    }
  };

  const test = async (provider) => {
    setTesting(provider);
    setTestResult((r) => ({ ...r, [provider]: null }));
    try {
      // Nur die relevanten Felder mitschicken, plus die Zwischenspeicherung
      // für noch ungespeicherte Werte.
      const relevant =
        provider === "ollama"
          ? { ollama_base_url: draft.ollama_base_url, ollama_model: draft.ollama_model }
          : {
              openrouter_base_url: draft.openrouter_base_url,
              openrouter_model: draft.openrouter_model,
              openrouter_api_key: draft.openrouter_api_key,
            };
      const resp = await fetch("/api/settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, values: relevant }),
      });
      const body = await resp.json();
      setTestResult((r) => ({ ...r, [provider]: body }));
    } catch (e) {
      setTestResult((r) => ({
        ...r,
        [provider]: { ok: false, message: e.message },
      }));
    } finally {
      setTesting("");
    }
  };

  if (loading) return <p className="settings-hint">Lädt Einstellungen …</p>;
  if (!data) return null;

  const groups = data.groups || [];
  const meta = data.meta || {};

  return (
    <div className="settings">
      <div className="settings-head">
        <div>
          <h2>Einstellungen</h2>
          <p className="sub">
            Werden in der Datenbank gespeichert und überschreiben die Datei
            <code> /opt/domainradar/.env</code>. Änderungen greifen beim nächsten
            Lauf der Zeitpläne.
          </p>
        </div>
        <button className="primary" onClick={save} disabled={saving}>
          {saving ? "Speichert …" : "Speichern"}
        </button>
      </div>

      {msg ? (
        <p className={msg.kind === "ok" ? "msg-ok" : "msg-error"}>{msg.text}</p>
      ) : null}

      {groups.map((group) => (
        <section key={group} className="settings-group">
          <h3>{group}</h3>
          {Object.entries(meta)
            .filter(([, m]) => m.group === group)
            .map(([key, m]) => {
              const current = data.values[key];
              const isSecret = current && typeof current === "object" && "is_set" in current;
              const value = draft[key] ?? "";
              return (
                <div key={key} className="setting-row">
                  <label htmlFor={`set-${key}`}>{m.label}</label>
                  {isSecret ? (
                    <div className="secret-wrap">
                      <input
                        id={`set-${key}`}
                        type="password"
                        autoComplete="off"
                        placeholder={
                          current.is_set
                            ? `gespeichert (${current.preview})`
                            : "nicht gesetzt"
                        }
                        value={value}
                        onChange={(e) =>
                          setDraft((d) => ({ ...d, [key]: e.target.value }))
                        }
                      />
                      {current.is_set ? (
                        <span className="badge">gesetzt</span>
                      ) : (
                        <span className="badge off">fehlt</span>
                      )}
                    </div>
                  ) : typeof current === "boolean" || key === "respect_robots" ? (
                    <select
                      id={`set-${key}`}
                      value={value}
                      onChange={(e) =>
                        setDraft((d) => ({
                          ...d,
                          [key]: e.target.value === "1" ? 1 : 0,
                        }))
                      }
                    >
                      <option value="1">ja</option>
                      <option value="0">nein</option>
                    </select>
                  ) : (
                    <input
                      id={`set-${key}`}
                      type="text"
                      value={value}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d, [key]: e.target.value }))
                      }
                    />
                  )}
                  <p className="hint">{m.hint}</p>

                  {key === "ollama_base_url" || key === "openrouter_base_url" ? (
                    <div className="test-row">
                      <button
                        className="ghost"
                        onClick={() => test(key === "ollama_base_url" ? "ollama" : "openrouter")}
                        disabled={!!testing}
                      >
                        {testing === (key === "ollama_base_url" ? "ollama" : "openrouter")
                          ? "Prüfe …"
                          : "Verbindung testen"}
                      </button>
                      {testResult[
                        key === "ollama_base_url" ? "ollama" : "openrouter"
                      ] ? (
                        <span
                          className={
                            testResult[
                              key === "ollama_base_url" ? "ollama" : "openrouter"
                            ].ok
                              ? "msg-ok"
                              : "msg-error"
                          }
                        >
                          {
                            testResult[
                              key === "ollama_base_url" ? "ollama" : "openrouter"
                            ].message
                          }
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              );
            })}
        </section>
      ))}

      <p className="settings-note">
        API-Keys werden maskiert gespeichert und nie im Klartext an die Oberfläche
        zurückgegeben. Ein leer gelassenes Feld beim Speichern lässt den
        bestehenden Wert unverändert.
      </p>
    </div>
  );
}