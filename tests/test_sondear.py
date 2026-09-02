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
        institucion_privada=False, last_updated="2026-09-01T12:00:00+00:00",
        opc_nombre=None, owner_nombre=None,
        permite_carga_simultanea=False, formato=None, voltaje_maximo=None,
        amperaje_maximo=None, voltaje_actual=None, amperaje_actual=None,
        integrado=False):
    if opc_nombre is None:
        opc_nombre = operador
    if owner_nombre is None:
        owner_nombre = operador
    return {
        "location_id": location_id, "name": nombre, "commune": "Santiago",
        "region": "Metropolitana", "institucion_privada": institucion_privada,
        "parking_type": "PUBLICO",
        "owner": {"RUT": "995200007", "name": owner_nombre},
        "OPC": {"normalized_name": opc_nombre, "RUT": "995200007"},
        "evses": [{
            "evse_uid": location_id * 10, "last_updated": last_updated,
            "uso_exclusivo": False,
            "permite_carga_simultanea": permite_carga_simultanea,
            "connectors": [{
                "connector_id": connector_id, "status": estado, "standard": "CCS 2",
                "power_type": "DC", "max_electric_power": kw,
                "electric_power": 50 if estado == "OCUPADO" else 0, "soc": 40,
                "format": formato, "max_voltage": voltaje_maximo,
                "max_amperage": amperaje_maximo, "voltage": voltaje_actual,
                "amperage": amperaje_actual, "integrated": integrado,
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
    sondear.ARCHIVO_PALABRAS_CLAVE = tmp / "palabras_clave_marca.csv"
    (tmp / "palabras_clave_marca.csv").write_text(
        "palabra_clave,nombre_agrupado\nCOPEC,Copec Voltex\nENEL,Enel\n",
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


def test_carga_simultanea_y_potencia_en_vivo():
    tmp = Path("/tmp/pruebas_sondeo_4")
    entorno_limpio(tmp)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

    datos = [loc(1, "Sitio", "COPEC VOLTEX", 101, "OCUPADO", kw=60,
                 permite_carga_simultanea=True, formato="SOCKET",
                 voltaje_maximo=500, amperaje_maximo=125,
                 voltaje_actual=480, amperaje_actual=100, integrado=True)]
    cat, _ = sondear.procesar(datos, t0.isoformat(timespec="seconds"))
    fila = cat[0]

    assert str(fila["permite_carga_simultanea"]) == "1"
    assert fila["formato"] == "SOCKET"
    assert str(fila["voltaje_maximo"]) == "500"
    assert str(fila["amperaje_maximo"]) == "125"
    assert str(fila["voltaje_actual"]) == "480"
    assert str(fila["amperaje_actual"]) == "100"
    assert str(fila["potencia_actual_kw"]) == "50"  # electric_power en vivo, seteado por loc() cuando OCUPADO
    assert str(fila["porcentaje_bateria"]) == "40"
    assert str(fila["integrado"]) == "1"
    print("OK: carga simultanea y potencia en vivo quedan registradas en el catalogo")


def test_rescate_por_palabra_clave_cuando_opc_no_informado():
    tmp = Path("/tmp/pruebas_sondeo_5")
    entorno_limpio(tmp)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

    # OPC no informado y owner sin relacion (una municipalidad), pero el nombre
    # de la ubicacion delata la marca real.
    datos = [loc(1, "Estacionamiento COPEC Ruta 5", "Municipalidad de Rancagua", 201,
                 "DISPONIBLE", opc_nombre="Sin Operador Informado")]
    cat, _ = sondear.procesar(datos, t0.isoformat(timespec="seconds"))

    assert cat[0]["operador_agrupado"] == "Copec Voltex", \
        "sin OPC informado, debe rescatar la marca por el nombre de la ubicacion"
    print("OK: sin OPC informado, el nombre de la ubicacion rescata la marca real")


def test_rescate_nunca_pisa_opc_informado():
    tmp = Path("/tmp/pruebas_sondeo_6")
    entorno_limpio(tmp)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

    # El nombre de la ubicacion menciona COPEC, pero el OPC SI esta informado
    # y dice otra cosa: el OPC es el dato oficial (SEC) y nunca debe pisarse.
    datos = [loc(1, "Estacionamiento COPEC Ruta 5", "ENEL X", 202, "DISPONIBLE",
                 opc_nombre="ENEL X", owner_nombre="Municipalidad de Rancagua")]
    cat, _ = sondear.procesar(datos, t0.isoformat(timespec="seconds"))

    assert cat[0]["operador_agrupado"] == "ENEL X", \
        "el rescate por palabra clave no debe pisar un OPC que si esta informado"
    print("OK: el rescate por palabra clave nunca pisa un OPC informado")


def test_rescate_cae_a_owner_si_no_hay_palabra_clave_conocida():
    tmp = Path("/tmp/pruebas_sondeo_7")
    entorno_limpio(tmp)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

    # OPC no informado, pero el nombre de la ubicacion no contiene ninguna
    # palabra clave conocida: debe seguir cayendo al owner, como antes.
    datos = [loc(1, "Estacionamiento Central", "Municipalidad de Rancagua", 203,
                 "DISPONIBLE", opc_nombre="Sin Operador Informado")]
    cat, _ = sondear.procesar(datos, t0.isoformat(timespec="seconds"))

    assert cat[0]["operador_agrupado"] == "Municipalidad de Rancagua", \
        "sin palabra clave conocida en el nombre, debe caer al owner igual que antes"
    print("OK: sin palabra clave conocida en el nombre, cae al owner como antes")


if __name__ == "__main__":
    test_ciclo_disponible_ocupado_disponible()
    test_columnas_calzan_con_la_planilla()
    test_tramos_de_potencia()
    test_retiro_despues_de_la_gracia()
    test_el_crudo_se_guarda_y_se_puede_releer()
    test_location_malformada_no_rompe()
    test_carga_simultanea_y_potencia_en_vivo()
    test_rescate_por_palabra_clave_cuando_opc_no_informado()
    test_rescate_nunca_pisa_opc_informado()
    test_rescate_cae_a_owner_si_no_hay_palabra_clave_conocida()
    print("\nTodas las pruebas pasaron.")
