"""
Prueba de humo del pipeline de ingesta, con datos ficticios (fixtures) que
imitan la forma real de la API. No requiere red: ingest_snapshot() es pura
y se le pasa el timestamp a mano para poder simular el paso del tiempo
sin tener que dormir de verdad.

Corre con: python3 tests/test_poller.py
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from poller import SCHEMA, ingest_snapshot, _extract_connectors  # noqa: E402


def loc(location_id, name, commune, region, operator_name, connector_id, status,
        electric_power=0, institucion_privada=False, standard="CCS 2", power_type="DC",
        max_kw=60, uso_exclusivo=False, last_updated="2026-09-01T12:00:00+00:00"):
    return {
        "location_id": location_id,
        "name": name,
        "commune": commune,
        "region": region,
        "institucion_privada": institucion_privada,
        "parking_type": "PUBLICO",
        "owner": {"RUT": "999", "name": operator_name},
        "OPC": {"normalized_name": operator_name, "RUT": "999"},
        "evses": [
            {
                "evse_uid": location_id * 10 + 1,
                "last_updated": last_updated,
                "uso_exclusivo": uso_exclusivo,
                "connectors": [
                    {
                        "connector_id": connector_id,
                        "status": status,
                        "standard": standard,
                        "power_type": power_type,
                        "max_electric_power": max_kw,
                        "electric_power": electric_power,
                        "voltage": 400 if status == "OCUPADO" else 0,
                        "amperage": 125 if status == "OCUPADO" else 0,
                        "soc": 55 if status == "OCUPADO" else 0,
                    }
                ],
            }
        ],
    }


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def iso(dt):
    return dt.isoformat(timespec="seconds")


def test_full_session_lifecycle_and_kwh_estimate():
    conn = fresh_db()
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

    # Poll 1: conector A disponible, conector B (institucion_privada=True, para
    # confirmar que YA NO se descarta) tambien disponible.
    snap1 = [
        loc(1948, "COPEC VOLTEX - VIÑA PEREZ CRUZ", "Paine", "Metropolitana",
            "COPEC VOLTEX", connector_id=9001, status="DISPONIBLE",
            institucion_privada=True),
        loc(500, "ENEL X - Estacion X", "Santiago", "Metropolitana",
            "ENEL X", connector_id=9002, status="DISPONIBLE"),
    ]
    stats1 = ingest_snapshot(conn, snap1, ts=iso(t0))
    assert stats1["n_connectors"] == 2
    assert stats1["n_sessions_opened"] == 0

    row_a = conn.execute("SELECT * FROM connectors WHERE connector_id=9001").fetchone()
    assert row_a is not None, "el conector con institucion_privada=True SI debe quedar en el catalogo"
    assert row_a["operator_name"] == "COPEC VOLTEX"

    # Poll 2 (t+10 min): conector A empieza a cargar a 50 kW.
    t1 = t0 + timedelta(minutes=10)
    snap2 = [
        loc(1948, "COPEC VOLTEX - VIÑA PEREZ CRUZ", "Paine", "Metropolitana",
            "COPEC VOLTEX", connector_id=9001, status="OCUPADO", electric_power=50,
            institucion_privada=True),
        loc(500, "ENEL X - Estacion X", "Santiago", "Metropolitana",
            "ENEL X", connector_id=9002, status="DISPONIBLE"),
    ]
    stats2 = ingest_snapshot(conn, snap2, ts=iso(t1))
    assert stats2["n_sessions_opened"] == 1
    assert stats2["n_events"] == 1  # solo A cambio de estado

    open_sessions = conn.execute("SELECT * FROM sessions WHERE still_open=1").fetchall()
    assert len(open_sessions) == 1
    assert open_sessions[0]["connector_id"] == 9001

    # Poll 3 (t+20 min): sigue cargando, potencia bajo a 40 kW (tapering).
    t2 = t0 + timedelta(minutes=20)
    snap3 = [
        loc(1948, "COPEC VOLTEX - VIÑA PEREZ CRUZ", "Paine", "Metropolitana",
            "COPEC VOLTEX", connector_id=9001, status="OCUPADO", electric_power=40,
            institucion_privada=True),
        loc(500, "ENEL X - Estacion X", "Santiago", "Metropolitana",
            "ENEL X", connector_id=9002, status="DISPONIBLE"),
    ]
    ingest_snapshot(conn, snap3, ts=iso(t2))

    readings = conn.execute("SELECT * FROM power_readings WHERE connector_id=9001 ORDER BY ts").fetchall()
    assert [r["electric_power"] for r in readings] == [50, 40]

    # Poll 4 (t+30 min): termina la carga.
    t3 = t0 + timedelta(minutes=30)
    snap4 = [
        loc(1948, "COPEC VOLTEX - VIÑA PEREZ CRUZ", "Paine", "Metropolitana",
            "COPEC VOLTEX", connector_id=9001, status="DISPONIBLE",
            institucion_privada=True),
        loc(500, "ENEL X - Estacion X", "Santiago", "Metropolitana",
            "ENEL X", connector_id=9002, status="DISPONIBLE"),
    ]
    stats4 = ingest_snapshot(conn, snap4, ts=iso(t3))
    assert stats4["n_sessions_closed"] == 1

    sess = conn.execute("SELECT * FROM sessions WHERE connector_id=9001").fetchone()
    assert sess["still_open"] == 0
    assert sess["duration_min"] == 20.0  # de t1 a t3
    # Trapezoide entre (50kW,40kW) durante 10 min = 0.1667h -> (50+40)/2 * 0.1667 = 7.5 kWh
    assert abs(sess["kwh_estimated"] - 7.5) < 0.01
    assert sess["n_samples"] == 2
    assert sess["estimation_method"] == "trapezoidal"

    print("OK: test_full_session_lifecycle_and_kwh_estimate")


def test_retirement_closes_open_session():
    conn = fresh_db()
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

    snap1 = [loc(1, "Loc", "Comuna", "Region", "SAVE", connector_id=1, status="OCUPADO", electric_power=30)]
    ingest_snapshot(conn, snap1, ts=iso(t0))

    # 25 horas despues, el conector ya no aparece en la API (se fue de la red).
    t1 = t0 + timedelta(hours=25)
    ingest_snapshot(conn, [], ts=iso(t1))

    row = conn.execute("SELECT * FROM connectors WHERE connector_id=1").fetchone()
    assert row["active"] == 0
    assert row["estado_actual"] == "RETIRADO_DE_API"

    sess = conn.execute("SELECT * FROM sessions WHERE connector_id=1").fetchone()
    assert sess["still_open"] == 0
    assert sess["closed_reason"] == "retirado_de_api"

    print("OK: test_retirement_closes_open_session")


def test_grace_period_does_not_retire_early():
    conn = fresh_db()
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    snap1 = [loc(1, "Loc", "Comuna", "Region", "SAVE", connector_id=1, status="DISPONIBLE")]
    ingest_snapshot(conn, snap1, ts=iso(t0))

    t1 = t0 + timedelta(hours=2)  # dentro del periodo de gracia de 24h
    ingest_snapshot(conn, [], ts=iso(t1))

    row = conn.execute("SELECT * FROM connectors WHERE connector_id=1").fetchone()
    assert row["active"] == 1
    assert row["estado_actual"] == "DISPONIBLE"
    print("OK: test_grace_period_does_not_retire_early")


def test_single_sample_session_uses_constant_power_fallback():
    conn = fresh_db()
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    ingest_snapshot(conn, [loc(1, "Loc", "C", "R", "OP", 1, "DISPONIBLE")], ts=iso(t0))

    t1 = t0 + timedelta(minutes=5)
    ingest_snapshot(conn, [loc(1, "Loc", "C", "R", "OP", 1, "OCUPADO", electric_power=22)], ts=iso(t1))

    t2 = t0 + timedelta(minutes=15)  # 10 min cargando, sin una segunda lectura de potencia
    ingest_snapshot(conn, [loc(1, "Loc", "C", "R", "OP", 1, "DISPONIBLE")], ts=iso(t2))

    sess = conn.execute("SELECT * FROM sessions WHERE connector_id=1").fetchone()
    assert sess["estimation_method"] == "potencia_constante_1_muestra"
    assert abs(sess["kwh_estimated"] - (22 * 10 / 60)) < 0.01
    print("OK: test_single_sample_session_uses_constant_power_fallback")


def test_malformed_location_does_not_crash_parsing():
    conn = fresh_db()
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    good = loc(1, "Loc", "C", "R", "OP", 1, "DISPONIBLE")
    bogus = {"location_id": 2, "name": "Location rara", "evses": "esto-no-deberia-ser-un-string"}
    stats = ingest_snapshot(conn, [good, bogus], ts=iso(t0))
    assert stats["n_connectors"] == 1  # la buena entro, la mala se ignoro sin explotar
    print("OK: test_malformed_location_does_not_crash_parsing")


if __name__ == "__main__":
    test_full_session_lifecycle_and_kwh_estimate()
    test_retirement_closes_open_session()
    test_grace_period_does_not_retire_early()
    test_single_sample_session_uses_constant_power_fallback()
    test_malformed_location_does_not_crash_parsing()
    print("\nTodas las pruebas pasaron.")
