"""
Pruebas de cobertura.resumen_cobertura() (la reconstruccion completa de cobertura_diaria.csv), sin red ni archivos reales.
Corre con: python tests/test_cobertura.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))
import cobertura as resumir  # noqa: E402  (se conserva el alias: las pruebas llaman resumir.resumen_cobertura)

# En noviembre de 2026 Chile esta en UTC-3: las 23:00 del dia 10 en Chile son las 02:00 UTC del dia 11.
def utc(dia, hora, minuto=0, mes=11):
    return datetime(2026, mes, dia, hora, minuto, tzinfo=timezone.utc)


def corridas(*instantes, ok="1"):
    return [{"timestamp": t.isoformat(), "ok": ok} for t in instantes]


def cada(inicio, fin, minutos=1):
    """Corridas cada `minutos` desde inicio hasta fin (incluidos)."""
    out, t = [], inicio
    while t <= fin:
        out.append(t)
        t += timedelta(minutes=minutos)
    return out


def resumen(filas, ahora=None):
    """Por defecto `ahora` es 1 minuto despues de la ultima corrida (sondeo sano, sin caida en curso)."""
    marcas = [datetime.fromisoformat(f["timestamp"]) for f in filas if f["ok"] == "1"]
    return resumir.resumen_cobertura(filas, ahora or (max(marcas) + timedelta(minutes=1) if marcas else utc(1, 0)))


def por_fecha(filas):
    return {f["fecha"]: f for f in filas}


def test_sondeo_sano_solo_trae_el_dia_de_la_ultima_corrida():
    filas = resumen(corridas(*cada(utc(10, 12), utc(10, 15))))
    assert [f["fecha"] for f in filas] == ["2026-11-10"], filas
    assert filas[0]["hueco_max_min"] == 0 and filas[0]["ultima_corrida"] == utc(10, 15).isoformat(timespec="seconds")


def test_hueco_menor_a_15_min_no_se_registra():
    inst = cada(utc(10, 12), utc(10, 13)) + cada(utc(10, 13, 14), utc(10, 14))      # hueco de 14 min
    assert por_fecha(resumen(corridas(*inst)))["2026-11-10"]["hueco_max_min"] == 0


def test_hueco_dentro_de_un_dia():
    inst = cada(utc(10, 12), utc(10, 13)) + cada(utc(10, 14, 30), utc(10, 15))      # 90 min
    f = por_fecha(resumen(corridas(*inst)))["2026-11-10"]
    assert f["hueco_max_min"] == 90 and f["min_sin_cobertura"] == 90, f


def test_hueco_que_cruza_la_medianoche_cuenta_completo_para_los_dos_dias():
    """23:10 -> 00:20 hora de Chile (02:10 -> 03:20 UTC) = 70 min: 50 en un dia y 20 en el otro."""
    inst = cada(utc(11, 1), utc(11, 2, 10)) + cada(utc(11, 3, 20), utc(11, 4))
    d = por_fecha(resumen(corridas(*inst)))
    assert d["2026-11-10"]["hueco_max_min"] == 70 and d["2026-11-11"]["hueco_max_min"] == 70, d
    assert d["2026-11-10"]["min_sin_cobertura"] == 50 and d["2026-11-11"]["min_sin_cobertura"] == 20, d


def test_el_dia_de_reanudacion_cuenta_aunque_el_hueco_apenas_lo_roce():
    """Hueco de las 22:00 a las 00:00 en punto de Chile (01:00 -> 03:00 UTC): 120 min, todos del dia 10; el sondeo se reanuda el 11."""
    inst = cada(utc(11, 0), utc(11, 1)) + cada(utc(11, 3), utc(11, 4))
    d = por_fecha(resumen(corridas(*inst)))
    assert d["2026-11-10"]["hueco_max_min"] == 120 and d["2026-11-11"]["hueco_max_min"] == 120, d
    assert d["2026-11-11"]["min_sin_cobertura"] == 0, d


def test_las_corridas_fallidas_no_cubren():
    """Una corrida con ok = 0 (la API no respondio) no rompe el hueco."""
    inst = cada(utc(10, 12), utc(10, 13)) + cada(utc(10, 14, 30), utc(10, 15))
    filas = corridas(*inst) + corridas(utc(10, 13, 30), utc(10, 14), ok="0")
    assert por_fecha(resumen(filas))["2026-11-10"]["hueco_max_min"] == 90


def test_filas_con_timestamp_invalido_se_ignoran_y_sin_corridas_no_hay_filas():
    assert resumir.resumen_cobertura([]) == []
    assert resumir.resumen_cobertura([{"timestamp": "basura", "ok": "1"}, {"timestamp": "", "ok": "1"}]) == []


def test_hueco_largo_toca_todos_los_dias_del_medio():
    inst = cada(utc(10, 12), utc(10, 13)) + cada(utc(13, 12), utc(13, 13))          # del 10 a las 13:00 al 13 a las 12:00 UTC: 71 h = 4260 min
    d = por_fecha(resumen(corridas(*inst)))
    assert set(d) >= {"2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"} and all(d[f]["hueco_max_min"] == 4260 for f in d), d


def test_cambio_de_hora_de_chile():
    """El 6-sep-2026 Chile adelanta el reloj (a medianoche pasa a UTC-3): sigue repartiendo por fecha de Chile."""
    inst = cada(datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc), datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)) + \
        cada(datetime(2026, 9, 6, 6, 0, tzinfo=timezone.utc), datetime(2026, 9, 6, 7, 0, tzinfo=timezone.utc))
    d = por_fecha(resumen(corridas(*inst)))
    assert d["2026-09-05"]["hueco_max_min"] == 180 and d["2026-09-06"]["hueco_max_min"] == 180, d


def test_caida_en_curso_se_mide_hasta_ahora_y_marca_el_dia_de_hoy():
    """Ultima corrida a las 12:00; el archivo se genera a las 13:30 (90 min despues): hoy ya es un dia con caida."""
    filas = corridas(*cada(utc(10, 10), utc(10, 12)))
    d = por_fecha(resumir.resumen_cobertura(filas, utc(10, 13, 30)))
    assert d["2026-11-10"]["hueco_max_min"] == 90, d
    # y con 30 min de atraso (lo normal si el proceso corre cada 30 min) NO hay caida: no se marcan dias sanos
    d = por_fecha(resumir.resumen_cobertura(filas, utc(10, 12, 30)))
    assert d["2026-11-10"]["hueco_max_min"] == 30 and d["2026-11-10"]["hueco_max_min"] < 60, d


if __name__ == "__main__":
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for nombre, f in pruebas:
        f()
        print("OK:", nombre)
    print(f"\nTodas las pruebas pasaron ({len(pruebas)}).")
