"""
Pruebas del chequeo de salud, sin red y sin tocar corridas.csv real.
Corre con: python tests/test_chequeo_salud.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))
import chequeo_salud  # noqa: E402

AHORA = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def fila(minutos_atras, ok="1", error_tipo="", error_mensaje=""):
    ts = AHORA - timedelta(minutes=minutos_atras)
    return {
        "timestamp": ts.isoformat(timespec="seconds"),
        "ok": ok,
        "error_tipo": error_tipo,
        "error_mensaje": error_mensaje,
    }


def test_sin_corridas_todavia_no_es_falla():
    """Justo despues de configurar todo, antes de la primera corrida --
    no hay que alarmar a nadie por eso."""
    sano, mensaje = chequeo_salud.evaluar_salud([], AHORA)
    assert sano
    print("OK: sin corridas registradas todavia no se marca como falla")


def test_corridas_recientes_y_ok_es_sano():
    filas = [fila(5, ok="1"), fila(3, ok="1"), fila(1, ok="1")]
    sano, mensaje = chequeo_salud.evaluar_salud(filas, AHORA)
    assert sano
    assert "ok=1" in mensaje
    print("OK: corridas recientes y exitosas se marcan como sanas")


def test_sin_corridas_nuevas_hace_rato_es_falla():
    """Si la ultima corrida registrada es de hace mas del umbral, el
    sondeo dejo de correr (ej: el token del repo privado vencio y el
    checkout falla ANTES de que sondear.py alcance a escribir nada)."""
    filas = [fila(120, ok="1")]
    sano, mensaje = chequeo_salud.evaluar_salud(filas, AHORA)
    assert not sano
    assert "Sin corridas nuevas" in mensaje
    print("OK: sin corridas nuevas hace mas del umbral se marca como falla")


def test_una_corrida_fallida_aislada_no_es_falla():
    """Una corrida fallida suelta (la API caida un minuto) es normal --
    sondear.py ya reintenta solo. No hay que avisar por cada una."""
    filas = [fila(10, ok="1"), fila(5, ok="0", error_tipo="RuntimeError"), fila(1, ok="1")]
    sano, mensaje = chequeo_salud.evaluar_salud(filas, AHORA)
    assert sano
    print("OK: una falla aislada entre corridas exitosas no se marca como falla")


def test_todas_las_corridas_recientes_fallidas_es_falla():
    """Si TODAS las corridas de la ventana reciente fallaron (no solo la
    ultima), ahi si hay un problema real y persistente que avisar."""
    filas = [
        fila(50, ok="0", error_tipo="HTTPError", error_mensaje="HTTP 500"),
        fila(30, ok="0", error_tipo="HTTPError", error_mensaje="HTTP 500"),
        fila(10, ok="0", error_tipo="HTTPError", error_mensaje="HTTP 500"),
    ]
    sano, mensaje = chequeo_salud.evaluar_salud(filas, AHORA)
    assert not sano
    assert "fallaron todas" in mensaje
    print("OK: todas las corridas recientes fallidas se marca como falla")


def test_falla_vieja_fuera_de_la_ventana_no_cuenta():
    """Una racha de fallas de hace mucho tiempo, ya superada por corridas
    exitosas recientes, no debe seguir marcando falla para siempre."""
    filas = [
        fila(500, ok="0", error_tipo="HTTPError"),
        fila(490, ok="0", error_tipo="HTTPError"),
        fila(5, ok="1"),
    ]
    sano, mensaje = chequeo_salud.evaluar_salud(filas, AHORA)
    assert sano
    print("OK: una falla vieja ya superada por una corrida reciente exitosa no se arrastra")


if __name__ == "__main__":
    test_sin_corridas_todavia_no_es_falla()
    test_corridas_recientes_y_ok_es_sano()
    test_sin_corridas_nuevas_hace_rato_es_falla()
    test_una_corrida_fallida_aislada_no_es_falla()
    test_todas_las_corridas_recientes_fallidas_es_falla()
    test_falla_vieja_fuera_de_la_ventana_no_cuenta()
    print("\nTodas las pruebas pasaron.")
