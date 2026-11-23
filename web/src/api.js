// Schlanker fetch-Wrapper. Die API ist read-only, deshalb gibt es hier kein
// Retry- und kein Auth-Gerüst – Fehler laufen einfach in den Error-State.
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

export const api = {
  stats: () => getJSON("/api/stats"),
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