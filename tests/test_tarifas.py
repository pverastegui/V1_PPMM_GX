"""
Pruebas del historial de precios (tarifas), sin red. Corre con: python tests/test_tarifas.py

Lo que mas importa aca no es solo que registre bien los precios, sino que el sondeo NO cambie en nada:
  - con tarifas o sin ellas, el catalogo, los eventos y corridas.csv salen IGUALES;
  - si el paso de tarifas falla, el sondeo sigue igual (mismo resultado, mismo codigo de salida).
"""
import gzip
import importlib
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))
sys.path.insert(0, str(RAIZ / "tests"))
import sondear  # noqa: E402
from test_sondear import _RelojFalso, entorno_limpio, leer, loc  # noqa: E402


def con_tarifa(ubicacion, energia=None, fijo=None, tiempo=None, actualizada="2026-10-29T02:02:47+00:00"):
    """Le agrega a la primera conexion de la location la tarifa con la forma real de la SEC."""
    comps = []
    for nombre, precio in (("ENERGIA", energia), ("CARGO FIJO", fijo), ("TIEMPO", tiempo)):
        if precio is not None:
            comps.append({"order_number": len(comps) + 1, "tariff_dimension": nombre, "price": precio})
    ubicacion["evses"][0]["connectors"][0]["tariffs"] = [{
        "tariff_id": 1, "pse_id": 4, "last_updated": actualizada, "min_price": None, "max_price": None,
        "elements": {"price_components": comps},
    }]
    return ubicacion


T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def iso(minutos):
    return (T0 + timedelta(minutes=minutos)).isoformat(timespec="seconds")


def filas_tarifas():
    ruta = sondear.archivo_tarifas()
    return leer(ruta) if ruta.exists() else []


def test_primera_lectura_registra_cada_componente():
    entorno_limpio(Path("/tmp/pruebas_tarifas_1"))
    datos = [con_tarifa(loc(1, "Sitio", "COPEC VOLTEX", 11, "DISPONIBLE"), energia=440, fijo=0, tiempo=35)]
    assert sondear.registrar_tarifas(datos, iso(0)) == 3
    filas = {f["componente"]: f for f in filas_tarifas()}
    assert set(filas) == {"ENERGIA", "CARGO FIJO", "TIEMPO"}
    assert filas["ENERGIA"]["precio_anterior"] == "" and filas["ENERGIA"]["precio_nuevo"] == "440"
    assert filas["CARGO FIJO"]["precio_nuevo"] == "0"
    assert filas["ENERGIA"]["connector_id"] == "11"
    assert filas["ENERGIA"]["tarifa_actualizada"] == "2026-10-29T02:02:47+00:00"
    print("OK: la primera lectura registra un precio base por componente")


def test_sin_cambios_no_agrega_nada_y_repetir_no_duplica():
    entorno_limpio(Path("/tmp/pruebas_tarifas_2"))
    datos = [con_tarifa(loc(1, "Sitio", "COPEC VOLTEX", 11, "DISPONIBLE"), energia=440)]
    sondear.registrar_tarifas(datos, iso(0))
    # El mismo precio escrito distinto (440.0, "440") NO es un cambio
    datos2 = [con_tarifa(loc(1, "Sitio", "COPEC VOLTEX", 11, "OCUPADO"), energia=440.0)]
    datos3 = [con_tarifa(loc(1, "Sitio", "COPEC VOLTEX", 11, "DISPONIBLE"), energia="440")]
    assert sondear.registrar_tarifas(datos2, iso(5)) == 0
    assert sondear.registrar_tarifas(datos3, iso(10)) == 0
    assert len(filas_tarifas()) == 1
    print("OK: sin cambio de precio no se agrega nada (440, 440.0 y '440' son lo mismo)")


def test_un_cambio_guarda_el_precio_anterior():
    entorno_limpio(Path("/tmp/pruebas_tarifas_3"))
    sondear.registrar_tarifas([con_tarifa(loc(1, "S", "ENEL X", 11, "DISPONIBLE"), energia=339)], iso(0))
    n = sondear.registrar_tarifas([con_tarifa(loc(1, "S", "ENEL X", 11, "DISPONIBLE"), energia=439,
                                              actualizada="2026-10-27T01:00:00+00:00")], iso(60))
    assert n == 1
    ultima = filas_tarifas()[-1]
    assert (ultima["precio_anterior"], ultima["precio_nuevo"]) == ("339", "439")
    assert ultima["timestamp_deteccion"] == iso(60) and ultima["tarifa_actualizada"] == "2026-10-27T01:00:00+00:00"
    # y un segundo cambio encadena con el ultimo precio, no con el primero
    sondear.registrar_tarifas([con_tarifa(loc(1, "S", "ENEL X", 11, "DISPONIBLE"), energia=450)], iso(120))
    assert (filas_tarifas()[-1]["precio_anterior"], filas_tarifas()[-1]["precio_nuevo"]) == ("439", "450")
    print("OK: un cambio de precio guarda el precio anterior y se encadena con el ultimo conocido")


def test_conector_que_deja_de_informar_precio():
    entorno_limpio(Path("/tmp/pruebas_tarifas_4"))
    sondear.registrar_tarifas([con_tarifa(loc(1, "S", "ENEL X", 11, "DISPONIBLE"), energia=339),
                               con_tarifa(loc(2, "T", "ENEL X", 12, "DISPONIBLE"), energia=300)], iso(0))
    # El conector 11 viene SIN tarifa (se anota); el 12 NO viene en la respuesta (no se anota nada)
    n = sondear.registrar_tarifas([loc(1, "S", "ENEL X", 11, "DISPONIBLE")], iso(5))
    assert n == 1
    ultima = filas_tarifas()[-1]
    assert (ultima["connector_id"], ultima["precio_anterior"], ultima["precio_nuevo"]) == ("11", "339", "")
    # una respuesta vacia o rota no inventa perdidas
    assert sondear.registrar_tarifas([], iso(10)) == 0
    print("OK: perder la tarifa se anota; un conector ausente de la respuesta no cuenta")


def test_datos_malos_no_lanzan_excepcion():
    entorno_limpio(Path("/tmp/pruebas_tarifas_5"))
    roto = [None, 5, {"evses": None}, {"evses": [{"connectors": [{"connector_id": 1, "tariffs": [None]}]}]},
            {"evses": [{"connectors": [{"connector_id": 2, "tariffs": [{"elements": {"price_components": [
                {"tariff_dimension": "ENERGIA", "price": "no-es-numero"},
                {"tariff_dimension": None, "price": 5},
                {"tariff_dimension": "ENERGIA", "price": None}]}}]}]}]},
            con_tarifa(loc(3, "OK", "COPEC VOLTEX", 33, "DISPONIBLE"), energia=440)]
    try:
        n = sondear.registrar_tarifas(roto, iso(0))
    except Exception as exc:  # pragma: no cover
        raise AssertionError(f"no debia lanzar excepcion: {exc!r}")
    assert n == 1 and filas_tarifas()[0]["connector_id"] == "33", "lo bueno se registra aunque haya basura al lado"
    print("OK: datos malformados no lanzan excepcion y lo valido igual se registra")


def correr_main(datos_por_minuto, parche_tarifas=None):
    """Corre main() (el sondeo completo) una vez por cada (minuto, datos). Devuelve (codigos de salida, stderr)."""
    sondear.datetime = _RelojFalso
    if parche_tarifas:
        sondear.registrar_tarifas = parche_tarifas
    codigos, err = [], io.StringIO()
    for minuto, datos in datos_por_minuto:
        sondear.bajar_api = lambda d=datos: (d, 200, None, None)
        _RelojFalso._ahora = _RelojFalso(2026, 10, 1, 10, minuto, tzinfo=timezone.utc)
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            codigos.append(sondear.main())
    return codigos, err.getvalue()


def salida_del_sondeo(carpeta: Path):
    """Todo lo que el sondeo produce (sin el historial de precios): lo que NO debe cambiar."""
    out = {}
    for ruta in sorted(carpeta.rglob("*")):
        if ruta.is_file() and ruta.name != "tarifas_cambios.csv":
            out[str(ruta.relative_to(carpeta))] = (gzip.open(ruta, "rb").read() if ruta.suffix == ".gz" else ruta.read_bytes())
    return out


def secuencia(con_tarifas):
    """Tres lecturas del sondeo con cambios de estado; con o sin tarifas en los datos."""
    def sitio(estado, precio):
        s = loc(1, "Sitio", "COPEC VOLTEX", 11, estado)
        return con_tarifa(s, energia=precio, fijo=0) if con_tarifas else s
    return [(0, [sitio("DISPONIBLE", 440)]), (2, [sitio("OCUPADO", 440)]), (7, [sitio("DISPONIBLE", 395)])]


def test_el_sondeo_produce_lo_mismo_con_y_sin_tarifas():
    """Catalogo, eventos, crudo y corridas.csv deben salir IGUALES: las tarifas solo agregan su propio archivo."""
    try:
        base = Path("/tmp/pruebas_tarifas_6a"); entorno_limpio(base)
        codigos_a, _ = correr_main(secuencia(False))
        sin = salida_del_sondeo(base)
        assert not (base / "data" / "tarifas_cambios.csv").exists(), "sin tarifas en la API no debe crearse el archivo"

        importlib.reload(sondear)
        con = Path("/tmp/pruebas_tarifas_6b"); entorno_limpio(con)
        codigos_b, _ = correr_main(secuencia(True))
        # Los crudos difieren (los datos con tarifa pesan distinto): se compara todo menos el contenido del crudo
        assert [k for k in salida_del_sondeo(con)] == [k for k in sin], "mismos archivos (salvo tarifas_cambios.csv)"
        for k in sin:
            if k.endswith(".json.gz"):
                continue
            assert salida_del_sondeo(con)[k] == sin[k], f"{k} debe ser identico con o sin tarifas"
        assert codigos_a == codigos_b == [0, 0, 0]
        tarifas = leer(con / "data" / "tarifas_cambios.csv")
        assert [(t["componente"], t["precio_anterior"], t["precio_nuevo"]) for t in tarifas] == \
            [("CARGO FIJO", "", "0"), ("ENERGIA", "", "440"), ("ENERGIA", "440", "395")]
        print("OK: con o sin tarifas, el catalogo, los eventos y corridas.csv salen identicos; el historial queda aparte")
    finally:
        importlib.reload(sondear)


def test_si_falla_el_paso_de_tarifas_el_sondeo_sigue_igual():
    try:
        base = Path("/tmp/pruebas_tarifas_7a"); entorno_limpio(base)
        codigos_ok, _ = correr_main(secuencia(True))
        esperado = salida_del_sondeo(base)

        importlib.reload(sondear)
        roto = Path("/tmp/pruebas_tarifas_7b"); entorno_limpio(roto)

        def explota(datos, momento_iso):
            raise RuntimeError("falla simulada")

        codigos, err = correr_main(secuencia(True), parche_tarifas=explota)
        assert codigos == codigos_ok == [0, 0, 0], "el codigo de salida del sondeo no debe cambiar"
        assert "AVISO: no se pudieron registrar las tarifas" in err and "falla simulada" in err
        obtenido = salida_del_sondeo(roto)
        for k in esperado:
            if not k.endswith(".json.gz"):
                assert obtenido[k] == esperado[k], f"{k} debe salir igual aunque falle el paso de tarifas"
        assert {k for k in obtenido} == {k for k in esperado}, "el crudo se guarda igual"
        corridas = leer(roto / "data" / "corridas.csv")
        assert [c["ok"] for c in corridas] == ["1", "1", "1"], "las corridas se registran ok=1"
        print("OK: si el paso de tarifas falla, solo avisa: el sondeo (catalogo, eventos, crudo, corridas, salida) sigue igual")
    finally:
        importlib.reload(sondear)


def test_reconstruir_desde_snapshots_es_idempotente_y_coincide_con_el_sondeo():
    tmp = Path("/tmp/pruebas_tarifas_8")
    shutil.rmtree(tmp, ignore_errors=True)
    snaps = tmp / "snapshots"
    # tres snapshots (11:59, 12:04, 12:09 del 2026-10-03) con cambio de precio en el tercero
    lecturas = [("2026-10-03", "1159", 340), ("2026-10-03", "1204", 340), ("2026-10-03", "1209", 400)]
    for dia, hhmm, precio in lecturas:
        carpeta = snaps / dia
        carpeta.mkdir(parents=True, exist_ok=True)
        datos = [con_tarifa(loc(1, "S", "ENEL X", 11, "DISPONIBLE"), energia=precio)]
        with gzip.open(carpeta / f"{hhmm}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(datos, f)
    (carpeta / "1214.json.gz").write_bytes(b"esto no es un gzip")        # snapshot danado: se salta
    cmd = [sys.executable, str(RAIZ / "scripts" / "reconstruir_tarifas.py"), "--snapshots", str(snaps), "--data", str(tmp / "data")]

    simulacion = subprocess.run(cmd, capture_output=True, text=True)
    assert simulacion.returncode == 0 and "SIMULACION" in simulacion.stdout
    assert not (tmp / "data" / "tarifas_cambios.csv").exists(), "simular no escribe"

    r1 = subprocess.run(cmd + ["--aplicar"], capture_output=True, text=True)
    assert r1.returncode == 0, r1.stderr
    filas = leer(tmp / "data" / "tarifas_cambios.csv")
    assert [(f["timestamp_deteccion"], f["precio_anterior"], f["precio_nuevo"]) for f in filas] == \
        [("2026-10-03T11:59:00+00:00", "", "340"), ("2026-10-03T12:09:00+00:00", "340", "400")]
    assert "ilegibles" in r1.stdout and "1 ilegibles" in r1.stdout

    r2 = subprocess.run(cmd + ["--aplicar"], capture_output=True, text=True)       # segunda corrida: no duplica
    assert len(leer(tmp / "data" / "tarifas_cambios.csv")) == 2, r2.stdout
    print("OK: la reconstruccion usa la hora del snapshot, salta los danados y no duplica al repetirse")


if __name__ == "__main__":
    test_primera_lectura_registra_cada_componente()
    test_sin_cambios_no_agrega_nada_y_repetir_no_duplica()
    test_un_cambio_guarda_el_precio_anterior()
    test_conector_que_deja_de_informar_precio()
    test_datos_malos_no_lanzan_excepcion()
    test_el_sondeo_produce_lo_mismo_con_y_sin_tarifas()
    test_si_falla_el_paso_de_tarifas_el_sondeo_sigue_igual()
    test_reconstruir_desde_snapshots_es_idempotente_y_coincide_con_el_sondeo()
    print("\nTodas las pruebas de tarifas pasaron.")
