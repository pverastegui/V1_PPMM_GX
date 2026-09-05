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
        opc_informado=True, permite_carga_simultanea=True):
    opc = ({"normalized_name": operador, "RUT": "995200007"} if opc_informado
           else {"normalized_name": None, "RUT": None})
    return {
        "location_id": location_id, "name": nombre, "commune": "Santiago",
        "region": "Metropolitana", "institucion_privada": institucion_privada,
        "parking_type": "PUBLICO",
        "owner": {"RUT": "995200007", "name": operador},
        "OPC": opc,
        "evses": [{
            "evse_uid": location_id * 10, "last_updated": last_updated,
            "uso_exclusivo": False, "permite_carga_simultanea": permite_carga_simultanea,
            "connectors": [{
                "connector_id": connector_id, "status": estado, "standard": "CCS 2",
                "power_type": "DC", "max_electric_power": kw,
                "format": "CABLE", "max_voltage": 1000, "max_amperage": 200,
                "voltage": 400 if estado == "OCUPADO" else 0,
                "amperage": 125 if estado == "OCUPADO" else 0,
                "electric_power": 50 if estado == "OCUPADO" else 0, "soc": 40,
                "integrated": False,
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
    sondear.ARCHIVO_PALABRAS_CLAVE = tmp / "palabras_clave_marca.csv"
    (tmp / "mapeo_operadores.csv").write_text(
        "nombre_original,nombre_agrupado\nCOPEC VOLTEX,Copec Voltex\nCOPEC S.A.,Copec Voltex\n"
        "ENERLINK CHILE SPA,Enerlink\n",
        encoding="utf-8")
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


def test_permite_carga_simultanea_y_potencia_en_vivo():
    """Campos nuevos: si el cargador permite carga simultanea, y del
    conector la potencia/voltaje/amperaje EN VIVO ademas de los maximos."""
    tmp = Path("/tmp/pruebas_sondeo_4")
    entorno_limpio(tmp)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

    datos = [loc(1, "Sitio", "COPEC VOLTEX", 1, "OCUPADO", permite_carga_simultanea=False)]
    cat, _ = sondear.procesar(datos, t0.isoformat(timespec="seconds"))
    fila = cat[0]

    assert str(fila["permite_carga_simultanea"]) == "0"
    assert float(fila["potencia_actual_kw"]) == 50
    assert float(fila["voltaje_actual"]) == 400
    assert float(fila["amperaje_actual"]) == 125
    assert float(fila["porcentaje_bateria"]) == 40
    assert fila["formato"] == "CABLE"
    assert float(fila["voltaje_maximo"]) == 1000
    print("OK: se capturan carga simultanea y los valores en vivo del conector")


def test_rescate_por_palabra_clave_no_pisa_opc_informado():
    """Si el OPC esta informado se usa tal cual (mapeado), aunque el NOMBRE
    de la ubicacion contenga la palabra clave de OTRA marca -- el OPC nunca
    se pisa. Si el OPC NO esta informado, se busca la palabra clave en el
    nombre de la ubicacion ANTES de caer al owner."""
    tmp = Path("/tmp/pruebas_sondeo_5")
    entorno_limpio(tmp)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

    # Caso real que motivo el orden de las reglas: OPC = Enerlink (informado)
    # pero el nombre del sitio dice "Enel X" -> gana el OPC, siempre.
    sitio_con_opc = loc(1, "Enel X - Shell Laguna Caren", "ENERLINK CHILE SPA", 1, "DISPONIBLE")
    cat, _ = sondear.procesar([sitio_con_opc], t0.isoformat(timespec="seconds"))
    assert cat[0]["operador_agrupado"] == "Enerlink"

    # OPC NO informado, el owner es un tercero sin nada que ver (una
    # municipalidad), pero el nombre del sitio dice COPEC -> se rescata.
    sitio_sin_opc = loc(2, "COPEC Ruta 5", "Ilustre Municipalidad de Las Condes",
                         2, "DISPONIBLE", opc_informado=False)
    cat, _ = sondear.procesar([sitio_sin_opc], (t0 + timedelta(minutes=1)).isoformat(timespec="seconds"))
    assert cat[0]["operador_agrupado"] == "Copec Voltex"
    print("OK: el rescate por palabra clave no pisa un OPC informado, y rescata cuando no hay OPC")


class _RelojFalso(datetime):
    """Subclase de datetime que fija lo que devuelve .now() -- para probar
    main() (que sondea "el instante actual") sin depender del reloj real."""
    _ahora = None

    @classmethod
    def now(cls, tz=None):
        return cls._ahora


def test_crudo_se_guarda_solo_si_hay_eventos_o_toca_el_respaldo_cada_5_min():
    """El disparador de Apps Script solo ofrece intervalos fijos (1, 5, 10,
    15, 30 -- no hay "cada 2" ni "cada 3"), asi que el sondeo pasa a cada 1
    minuto. Guardar el snapshot crudo en TODAS esas corridas multiplicaria
    por 5 lo que pesa snapshots/ -- y como limpiar_crudos.py borra del
    arbol pero nunca reescribe el historial de git, ese peso de mas
    quedaria para siempre adentro del repositorio. Por eso main() solo
    guarda el crudo si esta corrida detecto un cambio de estado real, o si
    igual toca el respaldo periodico (cada 5 minutos) aunque no haya
    pasado nada."""
    tmp = Path("/tmp/pruebas_sondeo_6")
    entorno_limpio(tmp)

    datos_bajados = {"valor": None}
    sondear.bajar_api = lambda: (datos_bajados["valor"], 200, None, None)
    sondear.datetime = _RelojFalso

    def snapshots_guardados():
        return list(sondear.DIR_SNAPSHOTS.rglob("*.json.gz"))

    try:
        # Minuto 0 (multiplo de 5): ademas la primera lectura de un conector
        # siempre genera un evento (no tiene estado_anterior), asi que este
        # crudo se guarda por las dos razones a la vez.
        _RelojFalso._ahora = _RelojFalso(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        datos_bajados["valor"] = [loc(1, "Sitio", "COPEC VOLTEX", 1, "DISPONIBLE")]
        sondear.main()
        assert len(snapshots_guardados()) == 1

        # Minuto 1 (no multiplo de 5), mismo estado que antes -> sin eventos
        # nuevos -> no toca guardar crudo esta vez.
        _RelojFalso._ahora = _RelojFalso(2026, 9, 1, 10, 1, tzinfo=timezone.utc)
        sondear.main()
        assert len(snapshots_guardados()) == 1, \
            "sin cambios de estado y fuera del respaldo cada 5 min: no debe guardar un segundo crudo"

        # Minuto 2 (tampoco multiplo de 5), pero AHORA si cambia el estado
        # -> se guarda igual, porque lo que manda es que hubo un evento real.
        _RelojFalso._ahora = _RelojFalso(2026, 9, 1, 10, 2, tzinfo=timezone.utc)
        datos_bajados["valor"] = [loc(1, "Sitio", "COPEC VOLTEX", 1, "OCUPADO")]
        sondear.main()
        assert len(snapshots_guardados()) == 2

        corridas = leer(sondear.ARCHIVO_CORRIDAS)
        assert corridas[1]["archivo_crudo"] == "", "la corrida sin novedad no debe registrar archivo_crudo"
        assert corridas[2]["archivo_crudo"] != "", "la corrida con cambio de estado si debe registrar su archivo_crudo"
        print("OK: el crudo se guarda solo si hubo un evento real o toca el respaldo cada 5 minutos")
    finally:
        # Deshace los parches de este test (bajar_api y datetime) para no
        # afectar a las pruebas que corran despues.
        import importlib
        importlib.reload(sondear)


def test_variable_de_entorno_puede_apuntar_el_crudo_a_otra_carpeta():
    """sondear.yml usa esto para apuntar el crudo al checkout del repo
    privado cuando esta configurado (ver docstring, "SOBRE DONDE VIVE EL
    CRUDO Y LOS DATOS"). Sin la variable, vuelve al comportamiento de
    siempre. Esta prueba solo confirma que el mecanismo en si funciona."""
    import importlib
    import os

    otra_carpeta = Path("/tmp/pruebas_sondeo_7_otra_carpeta")
    shutil.rmtree(otra_carpeta, ignore_errors=True)

    os.environ["SONDEAR_DIR_SNAPSHOTS"] = str(otra_carpeta)
    try:
        importlib.reload(sondear)
        assert sondear.DIR_SNAPSHOTS == otra_carpeta, \
            "con la variable de entorno seteada, DIR_SNAPSHOTS debe apuntar ahi"
    finally:
        del os.environ["SONDEAR_DIR_SNAPSHOTS"]
        importlib.reload(sondear)  # vuelve a snapshots/ del repo, para las pruebas que siguen
        assert sondear.DIR_SNAPSHOTS == sondear.RAIZ / "snapshots", \
            "sin la variable de entorno, debe volver al comportamiento de siempre"

    print("OK: SONDEAR_DIR_SNAPSHOTS pisa donde se guarda el crudo (y sin ella, no cambia nada)")


def test_variable_de_entorno_puede_apuntar_los_datos_a_otra_carpeta():
    """Mismo mecanismo que SONDEAR_DIR_SNAPSHOTS, pero para data/ (catalogo,
    eventos, corridas) -- sondear.yml apunta las dos al mismo checkout del
    repo privado, para que NINGUN dato real quede en este repo publico."""
    import importlib
    import os

    otra_carpeta = Path("/tmp/pruebas_sondeo_7b_otra_carpeta")
    shutil.rmtree(otra_carpeta, ignore_errors=True)

    os.environ["SONDEAR_DIR_DATA"] = str(otra_carpeta)
    try:
        importlib.reload(sondear)
        assert sondear.DIR_DATA == otra_carpeta, \
            "con la variable de entorno seteada, DIR_DATA debe apuntar ahi"
        assert sondear.ARCHIVO_CATALOGO == otra_carpeta / "catalogo.csv"
        assert sondear.DIR_EVENTOS == otra_carpeta / "eventos"
        assert sondear.ARCHIVO_CORRIDAS == otra_carpeta / "corridas.csv"
    finally:
        del os.environ["SONDEAR_DIR_DATA"]
        importlib.reload(sondear)  # vuelve a data/ del repo, para las pruebas que siguen
        assert sondear.DIR_DATA == sondear.RAIZ / "data", \
            "sin la variable de entorno, debe volver al comportamiento de siempre"

    print("OK: SONDEAR_DIR_DATA pisa donde se guardan catalogo/eventos/corridas (y sin ella, no cambia nada)")


def test_repo_privado_solo_cambia_el_destino_no_el_racionamiento():
    """Con el repo privado configurado (DIR_SNAPSHOTS apunta afuera de este
    repo), el crudo sigue racionandose exactamente igual que sin el (evento
    real, o cada 5 minutos de respaldo) -- guardar TODO sin filtrar pesaria
    demasiado incluso en un repo dedicado solo a esto. Lo unico que cambia
    es DONDE queda el archivo cuando si toca guardarlo."""
    tmp = Path("/tmp/pruebas_sondeo_8")
    entorno_limpio(tmp)

    # Simula el escenario real: el crudo apunta a otra carpeta (el
    # equivalente al checkout del repo privado), no a snapshots/ de este
    # mismo repo.
    otra_carpeta = tmp / "crudo-privado" / "snapshots"
    sondear.DIR_SNAPSHOTS = otra_carpeta

    datos_bajados = {"valor": [loc(1, "Sitio", "COPEC VOLTEX", 1, "DISPONIBLE")]}
    sondear.bajar_api = lambda: (datos_bajados["valor"], 200, None, None)
    sondear.datetime = _RelojFalso

    def snapshots_guardados():
        return list(otra_carpeta.rglob("*.json.gz"))

    try:
        # Minuto 0: primera lectura, genera evento -> se guarda, y queda en
        # la carpeta del repo privado (no en snapshots/ de este repo).
        _RelojFalso._ahora = _RelojFalso(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        sondear.main()
        assert len(snapshots_guardados()) == 1
        assert not (tmp / "snapshots").exists() or not list((tmp / "snapshots").rglob("*.json.gz")), \
            "no debe quedar nada en snapshots/ de este repo cuando el privado esta configurado"

        # Minuto 1 (no es multiplo de 5), mismo estado que antes -> sin
        # eventos nuevos -> igual que sin repo privado, NO toca guardar.
        _RelojFalso._ahora = _RelojFalso(2026, 9, 1, 12, 1, tzinfo=timezone.utc)
        sondear.main()
        assert len(snapshots_guardados()) == 1, \
            "el repo privado no cambia el racionamiento: sin evento y fuera del respaldo, no debe guardar"

        # Minuto 5 (multiplo de 5): toca el respaldo periodico, se guarda
        # igual aunque no haya cambio de estado.
        _RelojFalso._ahora = _RelojFalso(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
        sondear.main()
        assert len(snapshots_guardados()) == 2

        corridas = leer(sondear.ARCHIVO_CORRIDAS)
        assert corridas[0]["archivo_crudo"] != "" and corridas[2]["archivo_crudo"] != ""
        assert corridas[1]["archivo_crudo"] == "", "la corrida sin novedad (minuto 1) no debe registrar archivo_crudo"
        print("OK: con el repo privado configurado, cambia el destino del crudo pero no el racionamiento")
    finally:
        import importlib
        importlib.reload(sondear)


def test_con_repo_privado_completo_no_queda_ningun_dato_en_este_repo():
    """El escenario real: sondear.yml apunta DIR_SNAPSHOTS y DIR_DATA al
    mismo checkout del repo privado. Esta prueba confirma que, en ese caso,
    catalogo.csv, los eventos, corridas.csv Y el crudo terminan todos
    afuera de este repo -- nada queda en las carpetas data/ ni snapshots/
    de este repo publico."""
    tmp = Path("/tmp/pruebas_sondeo_11")
    entorno_limpio(tmp)

    otra_raiz = tmp / "datos-privados"
    sondear.DIR_SNAPSHOTS = otra_raiz / "snapshots"
    sondear.DIR_DATA = otra_raiz / "data"
    sondear.ARCHIVO_CATALOGO = sondear.DIR_DATA / "catalogo.csv"
    sondear.DIR_EVENTOS = sondear.DIR_DATA / "eventos"
    sondear.ARCHIVO_CORRIDAS = sondear.DIR_DATA / "corridas.csv"

    datos_bajados = {"valor": [loc(1, "Sitio", "COPEC VOLTEX", 1, "DISPONIBLE")]}
    sondear.bajar_api = lambda: (datos_bajados["valor"], 200, None, None)
    sondear.datetime = _RelojFalso

    try:
        _RelojFalso._ahora = _RelojFalso(2026, 9, 1, 13, 0, tzinfo=timezone.utc)
        sondear.main()

        assert sondear.ARCHIVO_CATALOGO.exists(), "catalogo.csv debe existir, pero en el repo privado"
        assert sondear.ARCHIVO_CORRIDAS.exists(), "corridas.csv debe existir, pero en el repo privado"
        assert list((otra_raiz / "snapshots").rglob("*.json.gz")), "el crudo debe existir, pero en el repo privado"

        # entorno_limpio() crea data/ y snapshots/ vacias como parte de su
        # propio montaje (no por accion de main()) -- lo que importa es que
        # sigan VACIAS: main() no debe haber escrito nada ahi.
        assert not any((tmp / "data").iterdir()), "data/ de este repo publico debe quedar vacia"
        assert not any((tmp / "snapshots").iterdir()), "snapshots/ de este repo publico debe quedar vacia"
        print("OK: con el repo privado completo configurado, ningun dato (ni crudo ni catalogo/eventos/corridas) queda en este repo")
    finally:
        import importlib
        importlib.reload(sondear)


if __name__ == "__main__":
    test_ciclo_disponible_ocupado_disponible()
    test_columnas_calzan_con_la_planilla()
    test_tramos_de_potencia()
    test_retiro_despues_de_la_gracia()
    test_el_crudo_se_guarda_y_se_puede_releer()
    test_location_malformada_no_rompe()
    test_permite_carga_simultanea_y_potencia_en_vivo()
    test_rescate_por_palabra_clave_no_pisa_opc_informado()
    test_crudo_se_guarda_solo_si_hay_eventos_o_toca_el_respaldo_cada_5_min()
    test_variable_de_entorno_puede_apuntar_el_crudo_a_otra_carpeta()
    test_variable_de_entorno_puede_apuntar_los_datos_a_otra_carpeta()
    test_repo_privado_solo_cambia_el_destino_no_el_racionamiento()
    test_con_repo_privado_completo_no_queda_ningun_dato_en_este_repo()
    print("\nTodas las pruebas pasaron.")
