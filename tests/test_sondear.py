"""
Pruebas del sondeo, sin red: se le pasan datos falsos con la forma real de la API.
Corre con: python tests/test_sondear.py
"""
import csv
import gzip
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))
import sondear  # noqa: E402


def loc(location_id, nombre, operador, connector_id, estado, kw=60,
        institucion_privada=False, last_updated="2026-09-01T12:00:00+00:00"):
    return {
        "location_id": location_id, "name": nombre, "commune": "Santiago",
        "region": "Metropolitana", "institucion_privada": institucion_privada,
        "parking_type": "PUBLICO",
        "owner": {"RUT": "995200007", "name": operador},
        "OPC": {"normalized_name": operador, "RUT": "995200007"},
        "evses": [{
            "evse_uid": location_id * 10, "last_updated": last_updated,
            "uso_exclusivo": False,
            "connectors": [{
                "connector_id": connector_id, "status": estado, "standard": "CCS 2",
                "power_type": "DC", "max_electric_power": kw,
                "electric_power": 50 if estado == "OCUPADO" else 0, "soc": 40,
            }],
        }],
    }


def entorno_limpio(tmp: Path):
    """Redirige todas las rutas del modulo a una carpeta temporal."""
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "data").mkdir(parents=True)
    (tmp / "snapshots").mkdir(parents=True)
    sondear.RAIZ = tmp
    sondear.DIR_SNAPSHOTS = tmp / "snapshots"
    sondear.DIR_DATA = tmp / "data"
    sondear.ARCHIVO_CATALOGO = tmp / "data" / "catalogo.csv"
    sondear.DIR_EVENTOS = tmp / "data" / "eventos"
    sondear.ARCHIVO_CORRIDAS = tmp / "data" / "corridas.csv"
    sondear.ARCHIVO_MAPEO = tmp / "mapeo_operadores.csv"
    (tmp / "mapeo_operadores.csv").write_text(
        "nombre_original,nombre_agrupado\nCOPEC VOLTEX,Copec Voltex\nCOPEC S.A.,Copec Voltex\n",
        encoding="utf-8")


def leer(ruta):
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_ciclo_disponible_ocupado_disponible():
    tmp = Path("/tmp/pruebas_sondeo_1")
    entorno_limpio(tmp)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

    # Sondeo 1: dos conectores libres. El segundo es institucion_privada=True,
    # para confirmar que YA NO se descarta.
    snap1 = [
        loc(1, "COPEC Chimbarongo", "COPEC VOLTEX", 101, "DISPONIBLE"),
        loc(2, "Mall Plaza Oeste", "ENEL X", 102, "DISPONIBLE", institucion_privada=True),
    ]
    cat, ev = sondear.procesar(snap1, t0.isoformat(timespec="seconds"))
    sondear.escribir_csv(sondear.ARCHIVO_CATALOGO, sondear.COLUMNAS_CATALOGO, cat)
    sondear.agregar_csv(sondear.archivo_eventos_del_mes(t0), sondear.COLUMNAS_EVENTOS, ev)

    assert len(cat) == 2, "el conector en institucion privada SI debe quedar"
    assert len(ev) == 2 and all(e["estado_anterior"] == "PRIMERA_LECTURA" for e in ev)
    assert cat[0]["operador_agrupado"] == "Copec Voltex", "debe aplicar el mapeo"
    print("OK: primera lectura registra ambos conectores y aplica el mapeo")

    # Sondeo 2 (+5 min): el de Copec pasa a OCUPADO -> esto es el inicio de una sesion
    t1 = t0 + timedelta(minutes=5)
    snap2 = [
        loc(1, "COPEC Chimbarongo", "COPEC VOLTEX", 101, "OCUPADO"),
        loc(2, "Mall Plaza Oeste", "ENEL X", 102, "DISPONIBLE", institucion_privada=True),
    ]
    cat, ev = sondear.procesar(snap2, t1.isoformat(timespec="seconds"))
    sondear.escribir_csv(sondear.ARCHIVO_CATALOGO, sondear.COLUMNAS_CATALOGO, cat)
    sondear.agregar_csv(sondear.archivo_eventos_del_mes(t0), sondear.COLUMNAS_EVENTOS, ev)

    assert len(ev) == 1
    assert ev[0]["estado_anterior"] == "DISPONIBLE" and ev[0]["estado_nuevo"] == "OCUPADO"
    print("OK: DISPONIBLE -> OCUPADO queda registrado (inicio de sesion)")

    # Sondeo 3 (+25 min): vuelve a DISPONIBLE -> fin de la sesion
    t2 = t0 + timedelta(minutes=25)
    cat, ev = sondear.procesar(snap1, t2.isoformat(timespec="seconds"))
    sondear.escribir_csv(sondear.ARCHIVO_CATALOGO, sondear.COLUMNAS_CATALOGO, cat)
    sondear.agregar_csv(sondear.archivo_eventos_del_mes(t0), sondear.COLUMNAS_EVENTOS, ev)
    assert ev[0]["estado_anterior"] == "OCUPADO" and ev[0]["estado_nuevo"] == "DISPONIBLE"

    # Asi es como la planilla cuenta transacciones: filas con estado_nuevo = OCUPADO
    filas = leer(sondear.archivo_eventos_del_mes(t0))
    transacciones = [f for f in filas if f["estado_nuevo"] == "OCUPADO"]
    assert len(transacciones) == 1, "debe haber exactamente 1 transaccion contada"
    print("OK: OCUPADO -> DISPONIBLE cierra el ciclo; se cuenta 1 transaccion")


def test_columnas_calzan_con_la_planilla():
    """Las columnas A-L deben quedar en el orden que esperan las formulas."""
    esperado = ["timestamp_deteccion", "connector_id", "operator_name", "estado_anterior",
                "estado_nuevo", "api_last_updated", "power_type", "max_electric_power",
                "standard", "location_name", "operador_agrupado", "tramo_potencia"]
    assert sondear.COLUMNAS_EVENTOS[:12] == esperado
    # C = operator_name (lo que buscaba el BUSCARV), E = estado_nuevo,
    # H = max_electric_power, K = operador_agrupado, L = tramo_potencia
    assert sondear.COLUMNAS_EVENTOS[2] == "operator_name"
    assert sondear.COLUMNAS_EVENTOS[4] == "estado_nuevo"
    assert sondear.COLUMNAS_EVENTOS[7] == "max_electric_power"
    assert sondear.COLUMNAS_EVENTOS[10] == "operador_agrupado"
    assert sondear.COLUMNAS_EVENTOS[11] == "tramo_potencia"
    print("OK: las columnas A-L calzan con las formulas de la planilla")


def test_tramos_de_potencia():
    casos = [(7, "7"), (8, "7"), (11, "(7-22]"), (22, "(7-22]"), (50, "(22-50]"),
             (60, "(50-150]"), (150, "(50-150]"), (180, "150"), (350, "150")]
    for kw, esperado in casos:
        assert sondear.tramo_potencia(kw) == esperado, f"{kw} kW -> esperaba {esperado}"
    assert sondear.tramo_potencia(None) == "desconocido"
    # El tramo alto NUNCA debe llevar '>' (rompe los conteos en Sheets)
    assert ">" not in sondear.tramo_potencia(350)
    print("OK: los tramos de potencia calzan con los de la planilla")


def test_retiro_despues_de_la_gracia():
    tmp = Path("/tmp/pruebas_sondeo_2")
    entorno_limpio(tmp)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

    cat, _ = sondear.procesar([loc(1, "Sitio", "SAVE", 1, "DISPONIBLE")], t0.isoformat(timespec="seconds"))
    sondear.escribir_csv(sondear.ARCHIVO_CATALOGO, sondear.COLUMNAS_CATALOGO, cat)

    # A las 2 horas de ausencia: sigue activo (dentro de la gracia de 24h)
    cat, ev = sondear.procesar([], (t0 + timedelta(hours=2)).isoformat(timespec="seconds"))
    assert str(cat[0]["activo"]) == "1" and not ev
    sondear.escribir_csv(sondear.ARCHIVO_CATALOGO, sondear.COLUMNAS_CATALOGO, cat)

    # A las 25 horas: se marca retirado
    cat, ev = sondear.procesar([], (t0 + timedelta(hours=25)).isoformat(timespec="seconds"))
    assert str(cat[0]["activo"]) == "0"
    assert ev[0]["estado_nuevo"] == "RETIRADO_DE_API"
    print("OK: el retiro respeta el periodo de gracia de 24h")


def test_el_crudo_se_guarda_y_se_puede_releer():
    tmp = Path("/tmp/pruebas_sondeo_3")
    entorno_limpio(tmp)
    datos = [loc(1, "Sitio", "COPEC VOLTEX", 1, "OCUPADO")]
    ruta = sondear.guardar_crudo(datos, datetime(2026, 9, 1, 14, 35, tzinfo=timezone.utc))

    assert ruta.name == "1435.json.gz"
    assert ruta.parent.name == "2026-09-01"
    with gzip.open(ruta, "rt", encoding="utf-8") as f:
        recuperado = json.load(f)
    assert recuperado == datos, "el crudo debe volver identico a como entro"
    print("OK: el crudo se guarda comprimido y se recupera intacto")


def test_location_malformada_no_rompe():
    buena = loc(1, "Sitio", "COPEC VOLTEX", 1, "DISPONIBLE")
    mala = {"location_id": 2, "name": "rara", "evses": "esto-no-es-una-lista"}
    salida = sondear.aplanar([buena, mala], {})
    assert len(salida) == 1
    print("OK: una location con forma rara no tumba el resto")


if __name__ == "__main__":
    test_ciclo_disponible_ocupado_disponible()
    test_columnas_calzan_con_la_planilla()
    test_tramos_de_potencia()
    test_retiro_despues_de_la_gracia()
    test_el_crudo_se_guarda_y_se_puede_releer()
    test_location_malformada_no_rompe()
    print("\nTodas las pruebas pasaron.")
