"""
Pruebas del paso "cobertura del sondeo" que corre dentro de sondear.py (cobertura.actualizar_cobertura), sin red.
Corre con: python tests/test_cobertura_sondeo.py

Lo que mas importa aca no es solo que el calculo sea correcto, sino que el sondeo y los datos NO cambien:
  - con el paso o sin el, catalogo, eventos, crudo, corridas.csv y tarifas salen IGUALES (el paso solo agrega su archivo);
  - si el paso falla, el sondeo sigue igual (mismo resultado, mismo codigo de salida, ninguna corrida fallida de mas);
  - actualizar de a una corrida da lo mismo que reconstruir todo, y si se pierden actualizaciones se corrige solo.
"""
import gzip
import importlib
import io
import random
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))
sys.path.insert(0, str(RAIZ / "tests"))
import cobertura  # noqa: E402
import sondear  # noqa: E402
from test_sondear import _RelojFalso, entorno_limpio, leer, loc  # noqa: E402

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
CABECERA = "timestamp,ok,http_status,n_locations,n_conectores,n_eventos_nuevos,archivo_crudo,error_tipo,error_mensaje\n"


def fila_ok(t):
    return f"{t.isoformat(timespec='seconds')},1,200,1,1,0,,,\n"


def fila_fallida(t):                 # el mensaje de error puede traer saltos de linea (viene de un traceback)
    return f'{t.isoformat(timespec="seconds")},0,,,,,,ValueError,"fallo | Traceback:\n  File x, line 1\nValueError: malo"\n'


def instantes(semilla=1, n=3000):
    """Corridas cada minuto con huecos de distinto largo (incluidos algunos que cruzan la medianoche de Chile)."""
    azar = random.Random(semilla)
    t, out = T0, []
    for _ in range(n):
        out.append(t)
        t += timedelta(minutes=azar.choice([1] * 60 + [5, 14, 16, 30, 59, 61, 95, 400, 1500]))
    return out


def test_ultimas_corridas_ok_ignora_fallidas_con_saltos_de_linea_y_lee_hacia_atras():
    d = Path("/tmp/pruebas_cobertura_1"); d.mkdir(exist_ok=True); ruta = d / "corridas.csv"
    texto = CABECERA + fila_ok(T0) + fila_ok(T0 + timedelta(minutes=1))
    texto += "".join(fila_fallida(T0 + timedelta(minutes=2 + i)) for i in range(400))      # racha larga de fallas (> 32 KB)
    ruta.write_text(texto, encoding="utf-8")
    assert ruta.stat().st_size > 32 * 1024
    assert cobertura.ultimas_corridas_ok(ruta, 2) == [T0.timestamp(), (T0 + timedelta(minutes=1)).timestamp()]
    ruta.write_text(CABECERA + fila_fallida(T0), encoding="utf-8")
    assert cobertura.ultimas_corridas_ok(ruta, 2) == []
    ruta.write_text(CABECERA + fila_ok(T0), encoding="utf-8")
    assert cobertura.ultimas_corridas_ok(ruta, 2) == [T0.timestamp()]
    print("OK: lee solo el final de corridas.csv, ignora corridas fallidas (aunque traigan saltos de linea) y retrocede si hace falta")


def correr(instantes_, ruta_cob, ruta_corr, cada=1, fallar_en=()):
    """Simula el sondeo: agrega una corrida a corridas.csv y llama al paso, una vez por corrida. Devuelve los modos."""
    modos = []
    ruta_corr.write_text(CABECERA, encoding="utf-8")
    for i, t in enumerate(instantes_):
        with ruta_corr.open("a", encoding="utf-8", newline="") as f:
            f.write(fila_ok(t))
        if i in fallar_en or i % cada:
            continue
        modos.append(cobertura.actualizar_cobertura(ruta_cob, ruta_corr))
    return modos


def esperado_de(ruta_corr, hasta):
    """Lo que daria reconstruir todo desde corridas.csv, escrito y leido igual que el archivo real (para comparar texto con texto)."""
    tmp = ruta_corr.parent / "esperado_tmp.csv"
    cobertura.escribir_cobertura(tmp, cobertura.resumen_cobertura(leer(ruta_corr), hasta))
    filas = leer(tmp)
    tmp.unlink()
    return filas


def test_actualizar_de_a_una_corrida_da_lo_mismo_que_reconstruir_todo():
    d = Path("/tmp/pruebas_cobertura_2"); d.mkdir(exist_ok=True)
    ruta_cob, ruta_corr = d / "cobertura_diaria.csv", d / "corridas.csv"
    ruta_cob.unlink(missing_ok=True)
    ins = instantes()
    modos = correr(ins, ruta_cob, ruta_corr)
    assert modos[0] == "reconstruida" and set(modos[1:]) == {"incremental"}, set(modos)
    assert leer(ruta_cob) == esperado_de(ruta_corr, ins[-1])
    assert any(float(f["hueco_max_min"]) >= 60 for f in leer(ruta_cob)), "el caso de prueba debe incluir caidas de 60 min o mas"
    print(f"OK: {len(ins)} corridas actualizadas una por una dan EXACTAMENTE lo mismo que reconstruir todo "
          f"({len(leer(ruta_cob))} dias en el archivo)")


def test_el_paso_tiene_un_tope_de_tiempo_y_el_sondeo_no_se_entera():
    """Si por una situacion extrana el paso se pasa de su presupuesto, se corta con un aviso: nunca retiene al sondeo."""
    original, presupuesto = cobertura.actualizar_cobertura, cobertura.PRESUPUESTO_SEG
    try:
        d = Path("/tmp/pruebas_cobertura_8"); d.mkdir(exist_ok=True)
        ruta_cob, ruta_corr = d / "cobertura_diaria.csv", d / "corridas.csv"
        ruta_cob.unlink(missing_ok=True)
        correr([T0 + timedelta(minutes=i * 20) for i in range(3000)], ruta_cob, ruta_corr, cada=10 ** 9)   # 3000 huecos de 20 min, sin actualizar
        ruta_cob.unlink(missing_ok=True)
        cobertura.PRESUPUESTO_SEG = 0.0                         # presupuesto agotado desde el primer instante
        try:
            cobertura.actualizar_cobertura(ruta_cob, ruta_corr)
            raise AssertionError("debia cortarse por tiempo")
        except TimeoutError as exc:
            assert "se corta para no retener al sondeo" in str(exc)
        assert not ruta_cob.exists() and cobertura._limite is None, "no deja nada a medias y el tope se desactiva"
        # dentro del sondeo: solo avisa, el resultado no cambia
        base = Path("/tmp/pruebas_cobertura_8b"); entorno_limpio(base)
        (base / "data" / "corridas.csv").write_text(ruta_corr.read_text(encoding="utf-8"), encoding="utf-8")   # historial largo: la 1.a corrida debe reconstruir
        codigos, err = correr_main(MINUTOS, datos_del_minuto, parche_cobertura=original)
        assert codigos == [0] * len(MINUTOS) and "AVISO: no se pudo actualizar la cobertura" in err and "TimeoutError" in err
        assert [c["ok"] for c in leer(base / "data" / "corridas.csv")][-len(MINUTOS):] == ["1"] * len(MINUTOS), "ninguna corrida fallida de mas"
        cobertura.PRESUPUESTO_SEG = presupuesto
        assert cobertura.actualizar_cobertura(ruta_cob, ruta_corr) == "reconstruida", "con el presupuesto normal funciona"
        print("OK: el paso tiene un tope de tiempo: si se pasa se corta con un aviso y el sondeo sigue igual")
    finally:
        cobertura.PRESUPUESTO_SEG = presupuesto; cobertura._limite = None; cobertura.actualizar_cobertura = original
        importlib.reload(sondear)


def test_el_archivo_no_crece_con_dias_sanos():
    """Solo quedan los dias con huecos y el dia de la ultima corrida: un sondeo sano durante 5 dias no agrega 5 filas."""
    d = Path("/tmp/pruebas_cobertura_2b"); d.mkdir(exist_ok=True)
    ruta_cob, ruta_corr = d / "cobertura_diaria.csv", d / "corridas.csv"
    ruta_cob.unlink(missing_ok=True)
    ins = [T0 + timedelta(minutes=5 * i) for i in range(5 * 288)]            # 5 dias seguidos, una corrida cada 5 min
    correr(ins, ruta_cob, ruta_corr, cada=7)                                  # el paso corre de vez en cuando
    cobertura.actualizar_cobertura(ruta_cob, ruta_corr)
    filas = leer(ruta_cob)
    assert len(filas) == 1 and float(filas[0]["hueco_max_min"]) == 0, filas
    assert filas == esperado_de(ruta_corr, ins[-1])
    print("OK: el archivo no crece con dias sanos (solo el dia de la ultima corrida)")


def test_si_se_pierden_actualizaciones_se_reconstruye_solo_y_no_inventa_caidas():
    d = Path("/tmp/pruebas_cobertura_3"); d.mkdir(exist_ok=True)
    ruta_cob, ruta_corr = d / "cobertura_diaria.csv", d / "corridas.csv"
    ruta_cob.unlink(missing_ok=True)
    # corridas cada minuto SIN huecos, pero el paso falla en las corridas 100 a 399 (3 h 20 min de corridas sin actualizar)
    ins = [T0 + timedelta(minutes=i) for i in range(600)]
    modos = correr(ins, ruta_cob, ruta_corr, fallar_en=range(100, 400))
    assert "reconstruida" in modos[1:], "al volver a funcionar debe reconstruir, no sumar un hueco falso"
    esperado = esperado_de(ruta_corr, ins[-1])
    assert leer(ruta_cob) == esperado
    assert all(float(f["hueco_max_min"]) == 0 for f in esperado), "las corridas siguieron cada minuto: no hubo caida"
    print("OK: si el paso falla varias corridas, la siguiente reconstruye todo y NO inventa una caida de 3 horas")


def test_archivo_ausente_o_corrupto_se_reconstruye_y_sin_cambios_no_reescribe():
    d = Path("/tmp/pruebas_cobertura_4"); d.mkdir(exist_ok=True)
    ruta_cob, ruta_corr = d / "cobertura_diaria.csv", d / "corridas.csv"
    ruta_cob.unlink(missing_ok=True)
    ruta_corr.write_text(CABECERA + fila_ok(T0) + fila_ok(T0 + timedelta(minutes=30)), encoding="utf-8")
    assert cobertura.actualizar_cobertura(ruta_cob, ruta_corr) == "reconstruida"
    bueno = ruta_cob.read_text(encoding="utf-8")
    assert cobertura.actualizar_cobertura(ruta_cob, ruta_corr) == "sin cambios" and ruta_cob.read_text(encoding="utf-8") == bueno
    for basura in ["", "no es un csv", "fecha,hueco_max_min\n2026-11-09,3\n", "fecha,hueco_max_min,min_sin_cobertura,ultima_corrida\n2026-11-09,x,y,z\n"]:
        ruta_cob.write_text(basura, encoding="utf-8")
        assert cobertura.actualizar_cobertura(ruta_cob, ruta_corr) == "reconstruida" and ruta_cob.read_text(encoding="utf-8") == bueno, repr(basura)
    assert cobertura.actualizar_cobertura(d / "no_existe" / "c.csv", d / "no_existe.csv") == "sin corridas"
    assert not list(d.glob("*.tmp")), "no debe quedar ningun archivo temporal"
    print("OK: archivo ausente o corrupto se reconstruye; sin corridas nuevas no se reescribe; no quedan temporales")


# ---------------------------------------------------------------- integracion con sondear.main()

def correr_main(minutos, datos, parche_cobertura=None, estado="DISPONIBLE"):
    sondear.datetime = _RelojFalso
    if parche_cobertura is not None:
        cobertura.actualizar_cobertura = parche_cobertura
    codigos, err = [], io.StringIO()
    for minuto in minutos:
        sondear.bajar_api = lambda d=datos(minuto): (d, 200, None, None)
        _RelojFalso._ahora = _RelojFalso(2026, 10, 1, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minuto)
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            codigos.append(sondear.main())
    return codigos, err.getvalue()


def datos_del_minuto(minuto):
    return [loc(1, "Sitio", "COPEC VOLTEX", 11, "OCUPADO" if minuto % 3 == 0 else "DISPONIBLE")]


def salida(carpeta: Path, sin=("cobertura_diaria.csv",)):
    out = {}
    for ruta in sorted(carpeta.rglob("*")):
        if ruta.is_file() and ruta.name not in sin:
            out[str(ruta.relative_to(carpeta))] = gzip.open(ruta, "rb").read() if ruta.suffix == ".gz" else ruta.read_bytes()
    return out


MINUTOS = [0, 1, 2, 3, 4, 5, 6, 7, 40, 41, 42]          # incluye un hueco de 33 minutos (del 7 al 40)


def test_el_sondeo_produce_lo_mismo_con_y_sin_el_paso_de_cobertura():
    """Catalogo, eventos, crudo, corridas.csv y tarifas deben salir byte a byte IGUALES: el paso solo agrega su archivo."""
    original = cobertura.actualizar_cobertura
    try:
        sin = Path("/tmp/pruebas_cobertura_5a"); entorno_limpio(sin)
        codigos_sin, _ = correr_main(MINUTOS, datos_del_minuto, parche_cobertura=lambda *a, **k: "desactivado")
        assert not (sin / "data" / "cobertura_diaria.csv").exists()
        esperado = salida(sin)

        importlib.reload(sondear); cobertura.actualizar_cobertura = original
        con = Path("/tmp/pruebas_cobertura_5b"); entorno_limpio(con)
        codigos_con, err = correr_main(MINUTOS, datos_del_minuto)
        assert codigos_con == codigos_sin == [0] * len(MINUTOS) and err == ""
        assert salida(con) == esperado, "todo lo demas debe salir identico, byte a byte"
        nuevos = {p.name for p in (con / "data").iterdir()} - {p.name for p in (sin / "data").iterdir()}
        assert nuevos == {"cobertura_diaria.csv"}, f"el paso solo debe agregar su archivo, agrego {nuevos}"
        filas = leer(con / "data" / "cobertura_diaria.csv")
        assert [(f["fecha"], f["hueco_max_min"]) for f in filas] == [("2026-10-01", "33.0")], filas
        assert filas[0]["ultima_corrida"] == "2026-10-01T10:42:00+00:00"
        print("OK: con o sin el paso, catalogo, eventos, crudo, corridas y tarifas salen byte a byte identicos; solo se agrega cobertura_diaria.csv")
    finally:
        importlib.reload(sondear); cobertura.actualizar_cobertura = original


def test_si_falla_el_paso_de_cobertura_el_sondeo_sigue_igual():
    original = cobertura.actualizar_cobertura
    try:
        base = Path("/tmp/pruebas_cobertura_6a"); entorno_limpio(base)
        codigos_ok, _ = correr_main(MINUTOS, datos_del_minuto)
        esperado = salida(base)

        importlib.reload(sondear); cobertura.actualizar_cobertura = original
        roto = Path("/tmp/pruebas_cobertura_6b"); entorno_limpio(roto)

        def explota(*a, **k):
            raise RuntimeError("falla simulada")

        codigos, err = correr_main(MINUTOS, datos_del_minuto, parche_cobertura=explota)
        assert codigos == codigos_ok == [0] * len(MINUTOS), "el codigo de salida del sondeo no debe cambiar"
        assert "AVISO: no se pudo actualizar la cobertura" in err and "falla simulada" in err
        assert salida(roto) == esperado, "todo lo demas debe salir igual aunque falle el paso"
        assert [c["ok"] for c in leer(roto / "data" / "corridas.csv")] == ["1"] * len(MINUTOS), "no se registra ninguna corrida fallida de mas"
        assert not (roto / "data" / "cobertura_diaria.csv").exists()
        print("OK: si el paso de cobertura falla, solo avisa: el sondeo (catalogo, eventos, crudo, corridas, salida) sigue igual")
    finally:
        importlib.reload(sondear); cobertura.actualizar_cobertura = original


def test_si_la_api_falla_la_cobertura_no_se_toca():
    original = cobertura.actualizar_cobertura
    try:
        d = Path("/tmp/pruebas_cobertura_7"); entorno_limpio(d)
        correr_main([0, 1], datos_del_minuto)
        antes = (d / "data" / "cobertura_diaria.csv").read_bytes()
        sondear.bajar_api = lambda: (None, 503, "HTTPError", "caida")
        _RelojFalso._ahora = _RelojFalso(2026, 10, 1, 11, 0, tzinfo=timezone.utc)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert sondear.main() == 1
        assert (d / "data" / "cobertura_diaria.csv").read_bytes() == antes
        assert [c["ok"] for c in leer(d / "data" / "corridas.csv")] == ["1", "1", "0"]
        print("OK: una corrida fallida (la API no responde) no toca la cobertura; el hueco se ve cuando vuelve a haber una corrida exitosa")
    finally:
        importlib.reload(sondear); cobertura.actualizar_cobertura = original


if __name__ == "__main__":
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for nombre, f in pruebas:
        f()
    print(f"\nTodas las pruebas de cobertura del sondeo pasaron ({len(pruebas)}).")
