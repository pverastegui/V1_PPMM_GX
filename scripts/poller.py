#!/usr/bin/env python3
"""
MONITOR DE CARGADORES PUBLICOS DE CHILE
Fuente: https://cargadorespublicos.cl/api/data
(Plataforma de interoperabilidad SEC, Decreto Supremo N12 - Ministerio de Energia)

Este script hace UNA lectura de la API, la compara contra el estado guardado
en SQLite, y actualiza:

  - connectors      : catalogo (una fila por conector visto alguna vez, nunca se borra)
  - status_events   : log de transiciones de estado (append-only)
  - power_readings  : lecturas de potencia instantanea SOLO mientras un conector
                       esta OCUPADO (para poder integrar energia sin guardar
                       millones de filas de conectores inactivos)
  - sessions        : una fila por sesion de carga detectada (abierta o cerrada),
                       con kWh estimado por integracion trapezoidal de power_readings
  - poll_runs       : registro de CADA ejecucion (exitosa o fallida) para tener
                       visibilidad historica de errores, en vez de logs efimeros

Diseñado para correr como un solo proceso corto (ideal para GitHub Actions con
cron), pero la funcion ingest_snapshot() es pura y testeable sin red: recibe
los datos ya parseados + un timestamp, así se puede probar con fixtures.

IMPORTANTE - decisiones de diseño (ver conversacion con Pauli):
  - NO se filtra por institucion_privada. Ese campo indica si el SITIO
    pertenece a una institucion privada (ej. un mall), no si el operador de
    carga es publico o privado. Filtrar por el excluia ~22% de los datos,
    incluyendo 11% de las propias locations de Copec Voltex. Se guarda el
    campo por si luego quieren usarlo, pero no se usa para descartar filas.
  - El operador se toma de OPC.normalized_name (campo oficial de la
    plataforma SEC), con fallback a owner.name solo si OPC no viene informado.
  - Toda llamada de red y todo parseo esta en try/except: una falla puntual
    de la API NUNCA debe tumbar el proceso completo ni perderse en silencio.
    Se registra siempre en poll_runs, exitosa o no.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

API_URL = "https://cargadorespublicos.cl/api/data"
REQUEST_TIMEOUT_S = 25
MAX_RETRIES = 3
RETRY_BACKOFF_S = 5  # se multiplica: 5s, 10s, 20s

# Umbral de "ausencia" antes de considerar un conector RETIRADO_DE_API y,
# si estaba OCUPADO, cerrar su sesion abierta igual (evita sesiones fantasma
# que quedan abiertas para siempre si el conector desaparece de la API).
GRACIA_AUSENCIA_S = 24 * 60 * 60  # 24 horas

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cargadores.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS connectors (
    connector_id INTEGER PRIMARY KEY,
    evse_uid INTEGER,
    location_id INTEGER,
    location_name TEXT,
    commune TEXT,
    region TEXT,
    operator_rut TEXT,
    operator_name TEXT,
    standard TEXT,
    power_type TEXT,
    max_electric_power REAL,
    parking_type TEXT,
    institucion_privada INTEGER,
    uso_exclusivo INTEGER,
    estado_actual TEXT,
    estado_desde TEXT,
    api_last_updated TEXT,
    first_seen TEXT,
    last_seen_api TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS status_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    connector_id INTEGER NOT NULL,
    operator_name TEXT,
    location_name TEXT,
    status_from TEXT,
    status_to TEXT NOT NULL,
    api_last_updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_events_connector ON status_events(connector_id);
CREATE INDEX IF NOT EXISTS idx_status_events_ts ON status_events(ts);

CREATE TABLE IF NOT EXISTS power_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    connector_id INTEGER NOT NULL,
    session_id INTEGER,
    status TEXT,
    electric_power REAL,
    voltage REAL,
    amperage REAL,
    soc REAL
);
CREATE INDEX IF NOT EXISTS idx_power_readings_session ON power_readings(session_id);
CREATE INDEX IF NOT EXISTS idx_power_readings_connector_ts ON power_readings(connector_id, ts);

CREATE TABLE IF NOT EXISTS sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    connector_id INTEGER NOT NULL,
    operator_name TEXT,
    location_name TEXT,
    commune TEXT,
    region TEXT,
    standard TEXT,
    power_type TEXT,
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    duration_min REAL,
    kwh_estimated REAL,
    avg_power_kw REAL,
    n_samples INTEGER,
    estimation_method TEXT,
    still_open INTEGER NOT NULL DEFAULT 1,
    closed_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_connector ON sessions(connector_id);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions(still_open);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);

CREATE TABLE IF NOT EXISTS poll_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    http_status INTEGER,
    n_locations INTEGER,
    n_connectors INTEGER,
    n_events INTEGER,
    n_sessions_opened INTEGER,
    n_sessions_closed INTEGER,
    error_type TEXT,
    error_message TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@dataclass
class FetchResult:
    ok: bool
    http_status: Optional[int] = None
    data: Optional[list] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None


def fetch_api() -> FetchResult:
    """Descarga y parsea la API. Nunca lanza excepcion hacia afuera:
    cualquier falla de red, timeout o JSON invalido vuelve como FetchResult(ok=False, ...).
    Reintenta con backoff exponencial ante fallas transitorias."""
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(API_URL, timeout=REQUEST_TIMEOUT_S)
            last_status = resp.status_code
            if resp.status_code != 200:
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
            else:
                data = resp.json()  # puede lanzar JSONDecodeError
                if not isinstance(data, list):
                    raise ValueError(f"Se esperaba una lista, llego {type(data).__name__}")
                return FetchResult(ok=True, http_status=resp.status_code, data=data)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            last_exc = exc

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_S * (2 ** (attempt - 1)))

    return FetchResult(
        ok=False,
        http_status=last_status,
        error_type=type(last_exc).__name__ if last_exc else "Unknown",
        error_message=str(last_exc) if last_exc else "fallo desconocido",
    )


def _extract_connectors(raw_locations: list) -> dict:
    """De la lista cruda de locations, arma un dict connector_id -> atributos planos.
    NO filtra por institucion_privada (ver docstring del modulo)."""
    out = {}
    for loc in raw_locations:
        try:
            owner = loc.get("owner") or {}
            opc = loc.get("OPC") or {}
            operator_name = opc.get("normalized_name")
            if not operator_name or operator_name == "Sin Operador Informado":
                operator_name = owner.get("name") or "Sin Operador Informado"

            for evse in (loc.get("evses") or []):
                for conn in (evse.get("connectors") or []):
                    cid = conn.get("connector_id")
                    if cid is None:
                        continue
                    out[cid] = {
                        "connector_id": cid,
                        "evse_uid": evse.get("evse_uid"),
                        "location_id": loc.get("location_id"),
                        "location_name": loc.get("name"),
                        "commune": loc.get("commune"),
                        "region": loc.get("region"),
                        "operator_rut": owner.get("RUT") or opc.get("RUT"),
                        "operator_name": operator_name,
                        "standard": conn.get("standard"),
                        "power_type": conn.get("power_type"),
                        "max_electric_power": conn.get("max_electric_power"),
                        "parking_type": loc.get("parking_type"),
                        "institucion_privada": bool(loc.get("institucion_privada")),
                        "uso_exclusivo": bool(evse.get("uso_exclusivo")),
                        "estado": (conn.get("status") or "DESCONOCIDO").upper(),
                        "api_last_updated": evse.get("last_updated"),
                        "electric_power": conn.get("electric_power"),
                        "voltage": conn.get("voltage"),
                        "amperage": conn.get("amperage"),
                        "soc": conn.get("soc"),
                    }
        except (AttributeError, TypeError):
            # Una location con forma inesperada no debe tumbar el resto del parseo.
            continue
    return out


def _close_session(conn: sqlite3.Connection, session_id: int, end_ts: str, reason: str) -> None:
    rows = conn.execute(
        "SELECT ts, electric_power FROM power_readings WHERE session_id = ? ORDER BY ts",
        (session_id,),
    ).fetchall()

    start_row = conn.execute("SELECT start_ts FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    start_ts = parse_iso(start_row["start_ts"])
    end_dt = parse_iso(end_ts)
    duration_min = None
    if start_ts and end_dt:
        duration_min = (end_dt - start_ts).total_seconds() / 60.0

    kwh = 0.0
    method = "sin_datos"
    n = len(rows)
    if n >= 2:
        for a, b in zip(rows, rows[1:]):
            t0, t1 = parse_iso(a["ts"]), parse_iso(b["ts"])
            p0, p1 = a["electric_power"] or 0.0, b["electric_power"] or 0.0
            if t0 and t1:
                dt_h = (t1 - t0).total_seconds() / 3600.0
                kwh += (p0 + p1) / 2.0 * dt_h
        method = "trapezoidal"
    elif n == 1 and duration_min is not None:
        # Solo una muestra de potencia: se asume constante durante toda la sesion.
        kwh = (rows[0]["electric_power"] or 0.0) * (duration_min / 60.0)
        method = "potencia_constante_1_muestra"
    elif duration_min is not None:
        method = "sin_lecturas_de_potencia"

    avg_power = None
    if rows:
        vals = [r["electric_power"] for r in rows if r["electric_power"] is not None]
        if vals:
            avg_power = sum(vals) / len(vals)

    conn.execute(
        """UPDATE sessions
           SET end_ts = ?, duration_min = ?, kwh_estimated = ?, avg_power_kw = ?,
               n_samples = ?, estimation_method = ?, still_open = 0, closed_reason = ?
           WHERE session_id = ?""",
        (end_ts, duration_min, round(kwh, 4), avg_power, n, method, reason, session_id),
    )


def ingest_snapshot(conn: sqlite3.Connection, raw_locations: list, ts: Optional[str] = None) -> dict:
    """Aplica una lectura ya parseada de la API al estado en SQLite.
    Pura (no hace red), por eso es testeable con fixtures."""
    ts = ts or now_iso()
    nuevo = _extract_connectors(raw_locations)

    anteriores = {
        row["connector_id"]: row
        for row in conn.execute("SELECT * FROM connectors").fetchall()
    }

    open_sessions = {
        row["connector_id"]: row
        for row in conn.execute("SELECT * FROM sessions WHERE still_open = 1").fetchall()
    }

    todos_ids = set(nuevo.keys()) | set(anteriores.keys())
    n_events = 0
    n_opened = 0
    n_closed = 0

    for cid in todos_ids:
        actual = nuevo.get(cid)
        previo = anteriores.get(cid)

        if actual is not None:
            estado_anterior = previo["estado_actual"] if previo else None
            estado_nuevo = actual["estado"]
            cambio_estado = estado_anterior != estado_nuevo

            if cambio_estado:
                conn.execute(
                    """INSERT INTO status_events
                       (ts, connector_id, operator_name, location_name, status_from, status_to, api_last_updated)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (ts, cid, actual["operator_name"], actual["location_name"],
                     estado_anterior or "PRIMERA_LECTURA", estado_nuevo, actual["api_last_updated"]),
                )
                n_events += 1

            # --- manejo de sesiones ---
            fue_ocupado = estado_anterior == "OCUPADO"
            es_ocupado = estado_nuevo == "OCUPADO"

            if es_ocupado and not fue_ocupado:
                cur = conn.execute(
                    """INSERT INTO sessions (connector_id, operator_name, location_name, commune, region,
                                              standard, power_type, start_ts, still_open)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (cid, actual["operator_name"], actual["location_name"], actual["commune"], actual["region"],
                     actual["standard"], actual["power_type"], ts),
                )
                open_sessions[cid] = {"session_id": cur.lastrowid}
                n_opened += 1

            if es_ocupado:
                sess = open_sessions.get(cid)
                if sess:
                    conn.execute(
                        """INSERT INTO power_readings (ts, connector_id, session_id, status, electric_power, voltage, amperage, soc)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (ts, cid, sess["session_id"], estado_nuevo, actual["electric_power"],
                         actual["voltage"], actual["amperage"], actual["soc"]),
                    )

            if fue_ocupado and not es_ocupado:
                sess = open_sessions.pop(cid, None)
                if sess:
                    _close_session(conn, sess["session_id"], ts, reason="cambio_estado")
                    n_closed += 1

            estado_desde = ts if cambio_estado else (previo["estado_desde"] if previo else ts)

            conn.execute(
                """INSERT INTO connectors
                   (connector_id, evse_uid, location_id, location_name, commune, region, operator_rut,
                    operator_name, standard, power_type, max_electric_power, parking_type,
                    institucion_privada, uso_exclusivo, estado_actual, estado_desde, api_last_updated,
                    first_seen, last_seen_api, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(connector_id) DO UPDATE SET
                     evse_uid=excluded.evse_uid, location_id=excluded.location_id,
                     location_name=excluded.location_name, commune=excluded.commune, region=excluded.region,
                     operator_rut=excluded.operator_rut, operator_name=excluded.operator_name,
                     standard=excluded.standard, power_type=excluded.power_type,
                     max_electric_power=excluded.max_electric_power, parking_type=excluded.parking_type,
                     institucion_privada=excluded.institucion_privada, uso_exclusivo=excluded.uso_exclusivo,
                     estado_actual=excluded.estado_actual, estado_desde=excluded.estado_desde,
                     api_last_updated=excluded.api_last_updated, last_seen_api=excluded.last_seen_api,
                     active=1""",
                (cid, actual["evse_uid"], actual["location_id"], actual["location_name"], actual["commune"],
                 actual["region"], actual["operator_rut"], actual["operator_name"], actual["standard"],
                 actual["power_type"], actual["max_electric_power"], actual["parking_type"],
                 int(actual["institucion_privada"]), int(actual["uso_exclusivo"]), estado_nuevo,
                 estado_desde, actual["api_last_updated"],
                 previo["first_seen"] if previo else ts, ts),
            )

        elif previo is not None and previo["active"]:
            # Conector que conociamos pero no vino en esta lectura.
            ultima_vez = parse_iso(previo["last_seen_api"]) or parse_iso(ts)
            ausente_s = (parse_iso(ts) - ultima_vez).total_seconds() if ultima_vez else 0
            if ausente_s > GRACIA_AUSENCIA_S:
                if previo["estado_actual"] == "OCUPADO":
                    sess = open_sessions.pop(cid, None)
                    if sess:
                        _close_session(conn, sess["session_id"], ts, reason="retirado_de_api")
                        n_closed += 1
                conn.execute(
                    """INSERT INTO status_events
                       (ts, connector_id, operator_name, location_name, status_from, status_to, api_last_updated)
                       VALUES (?, ?, ?, ?, ?, 'RETIRADO_DE_API', NULL)""",
                    (ts, cid, previo["operator_name"], previo["location_name"], previo["estado_actual"]),
                )
                conn.execute(
                    "UPDATE connectors SET estado_actual='RETIRADO_DE_API', estado_desde=?, active=0 WHERE connector_id=?",
                    (ts, cid),
                )
                n_events += 1
            # si sigue dentro del periodo de gracia, no se toca nada.

    return {
        "n_locations": len(raw_locations),
        "n_connectors": len(nuevo),
        "n_events": n_events,
        "n_sessions_opened": n_opened,
        "n_sessions_closed": n_closed,
    }


def run_once(db_path: Path = DEFAULT_DB_PATH) -> bool:
    """Punto de entrada para un solo ciclo de sondeo. Devuelve True si pudo
    leer la API (independiente de si hubo cambios), False si fallo."""
    conn = get_db(db_path)
    ts = now_iso()
    fetch = fetch_api()

    if not fetch.ok:
        conn.execute(
            """INSERT INTO poll_runs (ts, ok, http_status, error_type, error_message)
               VALUES (?, 0, ?, ?, ?)""",
            (ts, fetch.http_status, fetch.error_type, fetch.error_message),
        )
        conn.commit()
        conn.close()
        print(f"[{ts}] FALLO: {fetch.error_type}: {fetch.error_message}", file=sys.stderr)
        return False

    try:
        stats = ingest_snapshot(conn, fetch.data, ts)
        conn.execute(
            """INSERT INTO poll_runs (ts, ok, http_status, n_locations, n_connectors, n_events,
                                       n_sessions_opened, n_sessions_closed)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?)""",
            (ts, fetch.http_status, stats["n_locations"], stats["n_connectors"], stats["n_events"],
             stats["n_sessions_opened"], stats["n_sessions_closed"]),
        )
        conn.commit()
        print(f"[{ts}] OK: {stats}")
        return True
    except Exception as exc:  # noqa: BLE001 - a proposito: nunca debe escapar sin loguearse
        conn.rollback()
        conn.execute(
            """INSERT INTO poll_runs (ts, ok, http_status, error_type, error_message)
               VALUES (?, 0, ?, ?, ?)""",
            (ts, fetch.http_status, type(exc).__name__, f"{exc}\n{traceback.format_exc()[-1000:]}"),
        )
        conn.commit()
        print(f"[{ts}] ERROR procesando snapshot: {exc}", file=sys.stderr)
        traceback.print_exc()
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sondea la API una vez y actualiza el SQLite.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Ruta al archivo SQLite")
    args = parser.parse_args()

    ok = run_once(args.db)
    sys.exit(0 if ok else 1)
