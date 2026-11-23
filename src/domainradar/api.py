"""DomainRadar – FastAPI-Anwendung.

Read-only. Liefert die eigene `web/dist` als statisches Frontend aus und
darunter die JSON-API.

Sinnvolle Defaults: BIND ist `0.0.0.0`, damit die Web-UI aus dem LAN erreichbar
ist. Das ist bewusst so – aber ohne Authentifizierung. Wer den Port nach außen
öffnet, stellt eine öffentliche Beobachtungsdatenbank ins Netz. Der Installer
weist darauf hin.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .db import Database
from .domains import display_name

log = logging.getLogger("domainradar.api")

DB_PATH = os.environ.get("DOMAINRADAR_DB", "/opt/domainradar/data/domainradar.db")
WEB_DIST = Path(os.environ.get("DOMAINRADAR_WEB", "/opt/domainradar/web"))
HOST = os.environ.get("DOMAINRADAR_HOST", "0.0.0.0")
PORT = int(os.environ.get("DOMAINRADAR_PORT", "8477"))


def create_app(db_path: str = DB_PATH) -> FastAPI:
    app = FastAPI(
        title="DomainRadar",
        description="Lokaler Radar für neu beobachtete .de-Domains",
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    db = Database(db_path)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "database": db_path}

    @app.get("/api/stats")
    def stats():
        return db.stats()

    @app.get("/api/startups")
    def startups(
        view: str = Query("market", pattern="^(market|leads)$"),
        country: str | None = None,
        category: str | None = None,
        segment: str | None = Query(None, pattern="^(b2b|b2c)?$"),
        q: str | None = None,
        days: int | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        sort: str = Query("newest", pattern="^(newest|oldest|confidence)$"),
    ):
        result = db.search(
            view=view,
            country=country,
            category=category,
            segment=segment,
            q=q,
            days=days,
            limit=limit,
            offset=offset,
            sort=sort,
        )
        # Lesbare Form für IDNs ergänzen; `domain` bleibt der technische Key.
        for hit in result["hits"]:
            hit["display_name"] = display_name(hit["domain"])
        return result

    @app.get("/api/domain/{domain}")
    def domain_detail(domain: str):
        row = db.get_domain(domain)
        if row is None:
            raise HTTPException(status_code=404, detail="Domain unbekannt")
        changes = db.conn.execute(
            """SELECT kind, detected_at, detail FROM changes
                WHERE domain=? ORDER BY detected_at DESC LIMIT 50""",
            (domain,),
        ).fetchall()
        data = dict(row)
        data["domain"] = domain
        data["display_name"] = display_name(domain)
        return {**data, "changes": [dict(c) for c in changes]}

    @app.get("/api/changes")
    def changes(limit: int = Query(100, ge=1, le=500)):
        rows = db.conn.execute(
            """SELECT domain, kind, detected_at, detail FROM changes
                ORDER BY detected_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return {"hits": [dict(r) for r in rows]}

    @app.get("/api/categories")
    def categories():
        rows = db.conn.execute(
            """SELECT category, COUNT(*) AS count FROM domains
                WHERE category IS NOT NULL GROUP BY category ORDER BY count DESC"""
        ).fetchall()
        return {"hits": [dict(r) for r in rows]}

    # ---------- Feed ----------

    @app.get("/feed.xml", response_class=PlainTextResponse)
    def feed():
        rows = db.conn.execute(
            """SELECT domain, title, summary_de, first_seen FROM domains
                WHERE title IS NOT NULL
                ORDER BY first_seen DESC LIMIT 50"""
        ).fetchall()

        items = []
        for r in rows:
            title = _xml_escape(r["title"] or r["domain"])
            desc = _xml_escape(r["summary_de"] or "")
            items.append(
                f"<item><title>{title}</title>"
                f"<link>https://{r['domain']}</link>"
                f"<guid isPermaLink=\"false\">{r['domain']}</guid>"
                f"<description>{desc}</description></item>"
            )
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel>'
            "<title>DomainRadar</title>"
            f"<link>http://localhost:{PORT}/</link>"
            "<description>Neu beobachtete .de-Domains</description>"
            + "".join(items)
            + "</channel></rss>"
        )
        return PlainTextResponse(body, media_type="application/rss+xml")

    @app.get("/export/leads.csv", response_class=PlainTextResponse)
    def export_leads():
        rows = db.search(view="leads", limit=500)
        lines = ["domain;title;category;segment;company_name;legal_form;first_seen"]
        for r in rows["hits"]:
            lines.append(
                ";".join(
                    str(x if x is not None else "").replace(";", ",")
                    for x in (
                        r["domain"],
                        r["title"],
                        r["category"],
                        r["segment"],
                        r["company_name"],
                        r["legal_form"],
                        r["first_seen"],
                    )
                )
            )
        return PlainTextResponse(
            "\n".join(lines), media_type="text/csv; charset=utf-8"
        )

    # ---------- Statisches Frontend ----------

    dist = _resolve_web_dist()

    if dist is not None and (dist / "index.html").exists():
        assets = dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/")
        def index():
            return FileResponse(str(dist / "index.html"))

        @app.get("/{path:path}")
        def spa(path: str):
            # SPA-Fallback: unbekannte Pfade an die App geben
            candidate = dist / path
            if candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(dist / "index.html"))
    else:
        log.warning("Kein Web-Build gefunden – nur API (/api/startups)")

        @app.get("/")
        def index_fallback():
            return JSONResponse(
                {
                    "app": "DomainRadar",
                    "status": "ok",
                    "hint": "Web-Build fehlt. Bitte 'npm ci && npm run build' in web/ ausführen.",
                    "api": "/api/startups",
                }
            )

    return app


def _resolve_web_dist() -> Path | None:
    """Web-Build finden, ohne auf einen festen Installationspfad zu bestehen.

    Reihenfolge: Umgebungsvariable, Installationspfad, Repo-relative `web/dist`.
    So läuft die API sowohl im LXC als auch in der Entwicklung.
    """
    env_web = os.environ.get("DOMAINRADAR_WEB")
    candidates = []
    if env_web:
        candidates.append(Path(env_web))
    here = Path(__file__).resolve()
    # src/domainradar/api.py → Projektwurzel ist zwei Ebenen höher
    candidates.append(here.parent.parent.parent / "web" / "dist")
    candidates.append(Path("/opt/domainradar/web/dist"))

    for cand in candidates:
        if (cand / "index.html").exists():
            return cand
    # Fallback: Verzeichnis mit dem Build ausgeben, auch wenn index.html fehlt
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def create_app_for_env() -> FastAPI:
    """App mit Umgebungsvariablen bauen – für `uvicorn domainradar.api:app`.

    Bewusst erst beim Aufruf: so lässt sich das Modul importieren und testen,
    ohne dass ein Verzeichnis unter /opt existieren muss.
    """
    return create_app(DB_PATH)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(DB_PATH), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()