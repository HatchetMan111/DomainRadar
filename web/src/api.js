// Schlanker fetch-Wrapper. Neben read-only GETs gibt es POSTs für Settings
// und den Pipeline-Trigger – Fehler laufen einfach in den Error-State.
const BASE = "";

async function getJSON(path) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} für ${path}`);
  }
  return resp.json();
}

async function postJSON(path, body) {
  const resp = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: body === undefined ? "{}" : JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status} für ${path}`);
  return data;
}

export const api = {
  stats: () => getJSON("/api/stats"),
  runs: () => getJSON("/api/runs"),
  run: (stage) => postJSON(`/api/run/${encodeURIComponent(stage)}`),
  startups: (params = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") qs.set(k, v);
    });
    const suffix = qs.toString() ? `?${qs}` : "";
    return getJSON(`/api/startups${suffix}`);
  },
  domain: (domain) => getJSON(`/api/domain/${encodeURIComponent(domain)}`),
  changes: (limit = 100) => getJSON(`/api/changes?limit=${limit}`),
  categories: () => getJSON("/api/categories"),
};