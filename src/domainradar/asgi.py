"""ASGI-Einstieg für `uvicorn domainradar.api:app`.

Das Modul `domainradar.api` importiert keine Datenbank beim Laden – das
erledigt erst der Aufruf von `create_app`. So kann die API in Tests mit
eigener Datenbank gebaut werden, ohne dass /opt/domainradar existieren muss.

Für den Produktivbetrieb setzt `uvicorn` dieses `app`-Objekt um.
"""

from __future__ import annotations

from .api import create_app_for_env

app = create_app_for_env()