"""DomainRadar – FastAPI-Anwendung.

Read-only. Liefert die eigene `web/dist` als statisches Frontend aus und
darunter die JSON-API.

Sicherheitshinweise:
- Default-BIND ist `127.0.0.1` (nur lokal). Für LAN-Zugriff explizit
  `DOMAINRADAR_HOST=0.0.0.0` setzen.
- Optional: `DOMAINRADAR_API_TOKEN` in der Umgebung setzen, dann müssen
  alle /api/*-Requests einen `Authorization: Bearer <token>`-Header senden.
- CORS ist auf gleiche Origin beschränkt (kein Cross-Origin-Zugriff).
"""

from __future__ import annotations

import logging
import os
import secrets
import shlex
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .db import Database
from .domains import display_name

log = logging.getLogger("domainradar.api")

DB_PATH = os.environ.get("DOMAINRADAR_DB", "/opt/domainradar/data/domainradar.db")
WEB_DIST = Path(os.environ.get("DOMAINRADAR_WEB", "/opt/domainradar/web"))
# Default auf 127.0.0.1 – sicherer. Für LAN: explizit 0.0.0.0 setzen.
HOST = os.environ.get("DOMAINRADAR_HOST", "127.0.0.1")
PORT = int(os.environ.get("DOMAINRADAR_PORT", "8477"))
# Optionaler API-Token für Bearer-Auth (leer = keine Auth)
API_TOKEN = os.environ.get("DOMAINRADAR_API_TOKEN", "").strip()


def _check_auth(request: Request) -> None:
    """Bearer-Token prüfen, falls DOMAINRADAR_API_TOKEN gesetzt ist.

    Wenn kein Token konfiguriert ist, ist die API offen (nur für lokalen
    Betrieb auf 127.0.0.1 gedacht). Mit Token: konstanter Vergleich via
    secrets.compare_digest gegen Timing-Angriffe.
    """
    if not API_TOKEN:
        return  # keine Auth konfiguriert
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization: Bearer <token> fehlt",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = header.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, API_TOKEN):
        raise HTTPException(
            status_code=403,
            detail="Token ungültig",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app(db_path: str = DB_PATH) -> FastAPI:
    app = FastAPI(
        title="DomainRadar",
        description="Lokaler Radar für neu beobachtete .de-Domains",
        version="0.2.0",
    )

    # CORS: nur gleiche Origin (kein Cross-Origin). Für lokales Frontend
    # reicht das; wer ein externes Frontend anbinden will, kann
    # DOMAINRADAR_CORS_ORIGINS setzen.
    cors_origins_env = os.environ.get("DOMAINRADAR_CORS_ORIGINS", "").strip()
    allow_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()] if cors_origins_env else []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=["GET", "PUT", "POST"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )

    db = Database(db_path)

    # ── API-Endpunkte (mit optionaler Auth) ──────────────────────────────

    @app.get("/api/health")
    def health(request: Request):
        _check_auth(request)
        return {"status": "ok", "database": db_path, "version": "0.2.0"}

    @app.get("/api/stats")
    def stats(request: Request):
        _check_auth(request)
        return db.stats()

    @app.get("/api/startups")
    def startups(
        request: Request,
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
        _check_auth(request)
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
        for hit in result["hits"]:
            hit["display_name"] = display_name(hit["domain"])
        return result

    @app.get("/api/domain/{domain}")
    def domain_detail(request: Request, domain: str):
        _check_auth(request)
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
    def changes(request: Request, limit: int = Query(100, ge=1, le=500)):
        _check_auth(request)
        rows = db.conn.execute(
            """SELECT domain, kind, detected_at, detail FROM changes
                ORDER BY detected_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return {"hits": [dict(r) for r in rows]}

    @app.get("/api/categories")
    def categories(request: Request):
        _check_auth(request)
        rows = db.conn.execute(
            """SELECT category, COUNT(*) AS count FROM domains
                WHERE category IS NOT NULL GROUP BY category ORDER BY count DESC"""
        ).fetchall()
        return {"hits": [dict(r) for r in rows]}

    # ---------- Einstellungen ----------

    @app.get("/api/settings")
    def get_settings(request: Request):
        _check_auth(request)
        from . import config

        settings = config.load(db)
        return {
            "values": config.public_view(settings),
            "meta": config.FIELD_META,
            "groups": config.GROUP_ORDER,
            "defaults": config.DEFAULTS,
        }

    @app.put("/api/settings")
    def put_settings(request: Request, payload: dict):
        _check_auth(request)
        from . import config

        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON-Objekt erwartet")

        values = payload.get("values")
        if not isinstance(values, dict):
            raise HTTPException(status_code=422, detail="Feld 'values' fehlt")

        changed = config.save(db, values)
        return {
            "ok": True,
            "changed": changed,
            "values": config.public_view(config.load(db)),
        }

    @app.post("/api/settings/test")
    def test_setting(request: Request, payload: dict):
        _check_auth(request)
        from . import config

        provider = str(payload.get("provider", "")).strip().lower()
        if provider not in ("ollama", "openrouter"):
            raise HTTPException(
                status_code=422, detail="provider muss 'ollama' oder 'openrouter' sein"
            )

        settings = config.load(db)
        for key, value in (payload.get("values") or {}).items():
            if key in config.DEFAULTS and value is not None:
                settings[key] = value

        return config.test_connection(provider, settings)

    # ---------- Manueller Pipeline-Trigger ----------

    STAGES = ("collect", "enrich", "classify", "changes")
    run_dir = Path(DB_PATH).parent
    running: dict[str, subprocess.Popen] = {}

    def _stage_pid(stage: str) -> int | None:
        """PID der laufenden Stufe oder None; räumt tote PID-Dateien auf."""
        proc = running.get(stage)
        if proc is not None:
            if proc.poll() is None:
                return proc.pid
            running.pop(stage, None)
            (run_dir / f"run-{stage}.pid").unlink(missing_ok=True)
            return None
        pid_file = run_dir / f"run-{stage}.pid"
        if not pid_file.exists():
            return None
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pid_file.unlink(missing_ok=True)
            return None

    @app.get("/api/runs")
    def runs_status(request: Request):
        _check_auth(request)
        all_pid = _stage_pid("all")
        out = []
        for s in STAGES:
            pid = all_pid or _stage_pid(s)
            log_file = run_dir / ("run-all.log" if all_pid else f"run-{s}.log")
            tail: list[str] = []
            if log_file.exists():
                tail = log_file.read_text(errors="replace").splitlines()[-5:]
            out.append({"stage": s, "running": pid is not None, "pid": pid, "log_tail": tail})
        return {"ok": True, "runs": out}

    @app.post("/api/run/{stage}")
    def run_stage(stage: str, request: Request):
        _check_auth(request)
        stage = stage.strip().lower()
        if stage == "all":
            stages = list(STAGES)
        elif stage in STAGES:
            stages = [stage]
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Unbekannte Stufe: {stage!r} (erlaubt: {', '.join(STAGES)} oder 'all')",
            )
        for s in stages:
            pid = _stage_pid(s)
            if pid is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Stufe '{s}' läuft bereits (PID {pid})",
                )
        if _stage_pid("all") is not None:
            raise HTTPException(status_code=409, detail="'all' läuft bereits")
        label = "all" if stage == "all" else stage
        cmd = " && ".join(f"{shlex.quote(sys.executable)} -m domainradar.{s}" for s in stages)
        log_file = run_dir / f"run-{label}.log"
        with open(log_file, "ab", buffering=0) as lf:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", cmd],
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        running[label] = proc
        (run_dir / f"run-{label}.pid").write_text(str(proc.pid))
        log.info("Pipeline manuell gestartet (%s, PID %d)", cmd, proc.pid)
        return {"ok": True, "stage": label, "pid": proc.pid, "log": str(log_file)}

    # ---------- Feed & Export ----------

    @app.get("/feed.xml", response_class=PlainTextResponse)
    def feed(request: Request):
        _check_auth(request)
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
    def export_leads(request: Request):
        _check_auth(request)
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

    @app.get("/export/leads.json")
    def export_leads_json(request: Request):
        """Leads als JSON – maschinenlesbar für CRM-Import."""
        _check_auth(request)
        rows = db.search(view="leads", limit=500)
        return JSONResponse({"leads": rows["hits"], "total": rows["total"]})

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
                    "version": "0.2.0",
                    "hint": "Web-Build fehlt. Bitte 'npm ci && npm run build' in web/ ausführen.",
                    "api": "/api/startups",
                }
            )

    return app


def _resolve_web_dist() -> Path | None:
    """Web-Build finden, ohne auf einen festen Installationspfad zu bestehen."""
    env_web = os.environ.get("DOMAINRADAR_WEB")
    candidates = []
    if env_web:
        candidates.append(Path(env_web))
    here = Path(__file__).resolve()
    candidates.append(here.parent.parent.parent / "web" / "dist")
    candidates.append(Path("/opt/domainradar/web/dist"))

    for cand in candidates:
        if (cand / "index.html").exists():
            return cand
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
    """App mit Umgebungsvariablen bauen – für `uvicorn domainradar.asgi:app`."""
    return create_app(DB_PATH)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    if HOST == "0.0.0.0" and not API_TOKEN:
        log.warning(
            "API bindet an 0.0.0.0 ohne Authentifizierung (DOMAINRADAR_API_TOKEN leer). "
            "Nur im vertrauenswürdigen LAN betreiben oder Token setzen."
        )
    uvicorn.run(create_app(DB_PATH), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
