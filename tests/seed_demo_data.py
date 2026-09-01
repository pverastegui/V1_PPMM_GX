"""
Genera data/cargadores.db con datos SINTETICOS realistas (no reales) para
poder revisar visualmente el dashboard antes de conectarlo a la API real.
No usar este archivo en produccion - es solo para verificar build_dashboard.py.
"""
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from poller import SCHEMA, ingest_snapshot  # noqa: E402

random.seed(42)

OPERATORS = {
    "COPEC VOLTEX": 45,
    "ENEL X": 38,
    "Enerlink Chile SpA": 14,
    "SAVE": 9,
    "Sin Operador Informado": 20,
}

# OJO: esto escribe en tests/demo_cargadores.db, NUNCA en data/cargadores.db
# (esa ruta es la real, la va a llenar el pipeline de produccion).
DB_PATH = Path(__file__).resolve().parent / "demo_cargadores.db"


def build_connectors():
    conns = []
    cid = 9000
    for op, n in OPERATORS.items():
        for i in range(n):
            conns.append({"connector_id": cid, "operator": op, "loc": f"{op} - Sitio {i+1}",
                          "max_kw": random.choice([22, 50, 60, 150])})
            cid += 1
    return conns


def make_snapshot(connectors, occupied_ids, idle_status, ts):
    """idle_status: dict connector_id -> ultimo estado no-OCUPADO, se mantiene estable
    la gran mayoria de las veces (como en la API real) y solo ocasionalmente cambia."""
    locs = []
    for c in connectors:
        if c["connector_id"] in occupied_ids:
            status = "OCUPADO"
        else:
            prev = idle_status.get(c["connector_id"], "DISPONIBLE")
            if random.random() < 0.985:  # ~98.5% de las veces se mantiene igual entre sondeos
                status = prev
            else:
                status = random.choices(["DISPONIBLE", "FUERA DE LINEA", "NO DISPONIBLE"], weights=[70, 20, 10])[0]
            idle_status[c["connector_id"]] = status
        power = round(c["max_kw"] * random.uniform(0.5, 1.0), 1) if status == "OCUPADO" else 0
        locs.append({
            "location_id": c["connector_id"] // 10,
            "name": c["loc"],
            "commune": "Santiago",
            "region": "Metropolitana",
            "institucion_privada": random.random() < 0.2,
            "parking_type": "PUBLICO",
            "owner": {"RUT": "1", "name": c["operator"]},
            "OPC": {"normalized_name": c["operator"], "RUT": "1"},
            "evses": [{
                "evse_uid": c["connector_id"] * 10,
                "last_updated": ts,
                "uso_exclusivo": False,
                "connectors": [{
                    "connector_id": c["connector_id"],
                    "status": status,
                    "standard": "CCS 2",
                    "power_type": "DC",
                    "max_electric_power": c["max_kw"],
                    "electric_power": power,
                    "voltage": 400 if status == "OCUPADO" else 0,
                    "amperage": 100 if status == "OCUPADO" else 0,
                    "soc": random.randint(20, 90) if status == "OCUPADO" else 0,
                }],
            }],
        })
    return locs


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    connectors = build_connectors()
    start = datetime.now(timezone.utc) - timedelta(days=14)
    t = start
    step = timedelta(minutes=12)
    occupied = set()

    n_polls = int(timedelta(days=14) / step)
    idle_status = {}
    for i in range(n_polls):
        # cada operador tiene una tasa de ocupacion distinta, Copec Voltex mas alto (mas trafico)
        for c in connectors:
            in_use = c["connector_id"] in occupied
            base_rate = {"COPEC VOLTEX": 0.18, "ENEL X": 0.12}.get(c["operator"], 0.08)
            if in_use:
                if random.random() < 0.35:  # termina la sesion
                    occupied.discard(c["connector_id"])
            else:
                if random.random() < base_rate * 0.15:
                    occupied.add(c["connector_id"])

        ts = t.isoformat(timespec="seconds")
        snap = make_snapshot(connectors, occupied, idle_status, ts)

        # simular un par de fallas de sondeo (ej. la API caida un rato)
        if 500 < i < 506:
            conn.execute(
                "INSERT INTO poll_runs (ts, ok, http_status, error_type, error_message) VALUES (?, 0, 503, 'RuntimeError', 'HTTP 503')",
                (ts,),
            )
        else:
            stats = ingest_snapshot(conn, snap, ts=ts)
            conn.execute(
                """INSERT INTO poll_runs (ts, ok, http_status, n_locations, n_connectors, n_events,
                                           n_sessions_opened, n_sessions_closed)
                   VALUES (?, 1, 200, ?, ?, ?, ?, ?)""",
                (ts, stats["n_locations"], stats["n_connectors"], stats["n_events"],
                 stats["n_sessions_opened"], stats["n_sessions_closed"]),
            )
        t += step

    conn.commit()
    n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_readings = conn.execute("SELECT COUNT(*) FROM power_readings").fetchone()[0]
    print(f"Listo: {n_polls} polls simulados, {n_sessions} sesiones, {n_readings} lecturas de potencia.")
    conn.close()


if __name__ == "__main__":
    main()
