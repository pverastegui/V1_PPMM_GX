#!/usr/bin/env python3
"""
Cobertura del SONDEO: en que dias (fecha en hora de Chile) el sondeo estuvo caido, para que el visor pueda excluirlos.

Una "caida" es un hueco entre dos corridas EXITOSAS (ok = 1) seguidas de data/corridas.csv. Se mide el tiempo seguido sin
mirar y no la cantidad de corridas por dia, porque el ritmo del sondeo cambio (cada 5 min hasta el 6-nov-2026, cada minuto
desde el 7). Resultado: data/cobertura_diaria.csv, unas decenas de filas (en vez de las ~1.400 filas diarias de corridas.csv):

  fecha              dia en hora de Chile
  hueco_max_min      el hueco COMPLETO mas largo que toca ese dia (un hueco de 70 min que cruza la medianoche, 50 + 20,
                     es de 70 min para los dos dias), incluido el dia en que el sondeo se reanuda aunque el hueco apenas
                     lo roce: ahi llega de golpe la rafaga de cambios acumulados
  min_sin_cobertura  minutos de ese dia que caen dentro de huecos (solo informativo)
  ultima_corrida     la ultima corrida exitosa (igual en todas las filas)

Solo hay filas para los dias que tocan un hueco de HUECO_MIN_REGISTRO_MIN o mas, mas el dia de la ultima corrida.

Lo usan dos programas:
  - sondear.py, en CADA corrida (actualizar_cobertura): suma solo el hueco nuevo, asi el archivo esta al dia al minuto y no
    depende del proceso de resumenes (que corre cada varias horas). Va en un paso aislado: si falla, el sondeo sigue igual.
  - resumir.py (resumen_cobertura): lo reconstruye entero desde corridas.csv.
"""
from __future__ import annotations

import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ_CHILE = ZoneInfo("America/Santiago")
except Exception:  # pragma: no cover - si falta la base de datos de zonas
    TZ_CHILE = None

# Un hueco menor a esto es normal (el sondeo corre cada 1-5 min) y no se registra.
HUECO_MIN_REGISTRO_MIN = 15
COLUMNAS = ["fecha", "hueco_max_min", "min_sin_cobertura", "ultima_corrida"]
_cache_fecha_hora: dict[int, str] = {}


def fecha_de_hora_utc(k: int) -> str:
    """Hora UTC absoluta k (segundos / 3600) -> fecha en hora de Chile. Chile siempre tiene horas enteras de diferencia
    con UTC, asi que la fecha de cualquier instante depende solo de su hora UTC."""
    f = _cache_fecha_hora.get(k)
    if f is None:
        dt = datetime.fromtimestamp(k * 3600, timezone.utc)
        f = _cache_fecha_hora[k] = (dt.astimezone(TZ_CHILE) if TZ_CHILE is not None else dt).date().isoformat()
    return f


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def anotar_hueco(dias: dict, a: float, b: float) -> None:
    """Anota en `dias` el hueco entre las corridas a y b (segundos epoch), si es de HUECO_MIN_REGISTRO_MIN o mas.
    Cada dia que toca queda con el hueco COMPLETO, y el dia en que se reanuda (el de b) tambien cuenta."""
    hueco = (b - a) / 60
    if hueco < HUECO_MIN_REGISTRO_MIN:
        return
    por_dia: dict[str, float] = defaultdict(float)
    k = int(a // 3600)
    while k * 3600 < b:
        por_dia[fecha_de_hora_utc(k)] += (min(b, (k + 1) * 3600) - max(a, k * 3600)) / 60
        k += 1
    por_dia[fecha_de_hora_utc(int(b // 3600))] += 0.0       # el dia en que se reanuda tambien cuenta
    for fecha, minutos in por_dia.items():
        d = dias.setdefault(fecha, {"hueco_max_min": 0.0, "min_sin_cobertura": 0.0})
        d["hueco_max_min"] = max(d["hueco_max_min"], hueco)
        d["min_sin_cobertura"] += minutos


def _asegurar_dia(dias: dict, ts: float) -> None:
    dias.setdefault(fecha_de_hora_utc(int(ts // 3600)), {"hueco_max_min": 0.0, "min_sin_cobertura": 0.0})


def _filas(dias: dict, ultima: float) -> list[dict]:
    """Filas del archivo: los dias con algun hueco, mas el dia de la ultima corrida. Un dia sin huecos que ya no es el ultimo
    se descarta (sin esto, la actualizacion incremental dejaria una fila en cero por cada dia que el sondeo corrio)."""
    iso = _iso(ultima)
    dia_ultima = fecha_de_hora_utc(int(ultima // 3600))
    return [{"fecha": f, "hueco_max_min": round(d["hueco_max_min"], 2),
             "min_sin_cobertura": round(d["min_sin_cobertura"], 2), "ultima_corrida": iso}
            for f, d in sorted(dias.items()) if f == dia_ultima or d["hueco_max_min"] or d["min_sin_cobertura"]]


def resumen_cobertura(filas_corridas: list[dict], ahora: datetime | None = None) -> list[dict]:
    """Reconstruye la cobertura completa desde las filas de corridas.csv.

    Una caida que sigue en curso tambien cuenta: se mide de la ultima corrida a `ahora` (por defecto, la hora actual).
    Siempre incluye el dia de la ultima corrida (con 0 si no tuvo huecos), para que ultima_corrida nunca se pierda."""
    marcas = []
    for f in filas_corridas:
        if f.get("ok") != "1":
            continue
        try:
            dt = datetime.fromisoformat(f.get("timestamp") or "")
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        marcas.append(dt.timestamp())
    if not marcas:
        return []
    marcas.sort()
    ultima = marcas[-1]
    hasta_ahora = (ahora or datetime.now(timezone.utc)).timestamp()
    puntos = marcas + [hasta_ahora] if hasta_ahora > ultima else marcas     # el ultimo "hueco" es ultima corrida -> ahora
    dias: dict[str, dict] = {}
    for a, b in zip(puntos, puntos[1:]):
        anotar_hueco(dias, a, b)
    _asegurar_dia(dias, ultima)
    return _filas(dias, ultima)


# ---------------------------------------------------------------- actualizacion incremental (la usa sondear.py)

_FILA_OK = re.compile(r"^(\d{4}-\d{2}-\d{2}T[^,\s]+),1,")


def ultimas_corridas_ok(ruta: Path, n: int = 2) -> list[float]:
    """Las ultimas n corridas exitosas de corridas.csv (epoch, de la mas vieja a la mas nueva), leyendo solo el final del archivo.

    No se parsea como CSV a proposito: las filas fallidas pueden traer saltos de linea dentro del mensaje de error, y el final
    del archivo puede empezar en medio de una de ellas. Una fila exitosa siempre cabe en una linea y empieza con 'timestamp,1,'."""
    tam = ruta.stat().st_size
    bloque = 32 * 1024
    while True:
        ini = max(0, tam - bloque)
        with ruta.open("rb") as f:
            f.seek(ini)
            texto = f.read().decode("utf-8", errors="ignore")
        lineas = texto.splitlines()
        if ini > 0:
            lineas = lineas[1:]                      # la primera linea puede estar cortada
        marcas = []
        for linea in lineas:
            m = _FILA_OK.match(linea)
            if m:
                try:
                    marcas.append(datetime.fromisoformat(m.group(1)).timestamp())
                except ValueError:
                    pass
        if len(marcas) >= n or ini == 0:
            return marcas[-n:]
        bloque *= 4                                  # una racha larga de fallas: se lee mas hacia atras


def leer_cobertura(ruta: Path) -> tuple[dict, float] | None:
    """(dias, ultima_corrida) del archivo, o None si no existe o no se puede leer."""
    try:
        with ruta.open(encoding="utf-8", newline="") as f:
            filas = list(csv.DictReader(f))
        if not filas:
            return None
        ultima = datetime.fromisoformat(filas[0]["ultima_corrida"]).timestamp()
        # El archivo guarda minutos con 2 decimales, pero todo se mide en segundos enteros: se recuperan los segundos exactos
        # (el error de redondeo es < 0,5 s) para que sumar de a poco no acumule redondeos y de lo mismo que reconstruir todo.
        exacto = lambda minutos: round(float(minutos) * 60) / 60
        dias = {fila["fecha"]: {"hueco_max_min": exacto(fila["hueco_max_min"]),
                                "min_sin_cobertura": exacto(fila["min_sin_cobertura"])} for fila in filas}
        return dias, ultima
    except (OSError, ValueError, KeyError, TypeError):
        return None


def escribir_cobertura(ruta: Path, filas: list[dict]) -> None:
    """Escribe de forma atomica (archivo temporal + reemplazo): quien lea nunca ve el archivo a medias."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_name(ruta.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS)
        w.writeheader()
        w.writerows(filas)
    os.replace(tmp, ruta)


def actualizar_cobertura(ruta_cobertura: Path, ruta_corridas: Path, exigir_zona: bool = True) -> str:
    """Pone al dia cobertura_diaria.csv despues de una corrida. Debe llamarse DESPUES de agregar la corrida a corridas.csv.

    Normalmente solo suma el hueco entre las dos ultimas corridas exitosas (trabajo constante, sin importar cuanto crezca
    corridas.csv). Si el archivo no existe, no se puede leer, o no esta al dia con corridas.csv (por ejemplo porque este paso
    fallo en alguna corrida anterior), lo reconstruye entero: se corrige solo, sin dejar huecos falsos.
    Devuelve 'sin corridas', 'sin cambios', 'incremental' o 'reconstruida'."""
    if exigir_zona and TZ_CHILE is None:
        raise RuntimeError("no hay base de datos de zonas horarias (instalar tzdata): las fechas saldrian en UTC")
    if not ruta_corridas.exists():
        return "sin corridas"
    marcas = ultimas_corridas_ok(ruta_corridas, 2)
    if not marcas:
        return "sin corridas"
    actual = marcas[-1]
    previa = marcas[-2] if len(marcas) > 1 else None
    estado = leer_cobertura(ruta_cobertura)
    if estado is not None:
        dias, ultima = estado
        if abs(ultima - actual) < 0.5:
            return "sin cambios"
        if previa is not None and abs(ultima - previa) < 0.5:
            anotar_hueco(dias, previa, actual)
            _asegurar_dia(dias, actual)
            escribir_cobertura(ruta_cobertura, _filas(dias, actual))
            return "incremental"
    with ruta_corridas.open(encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f))
    escribir_cobertura(ruta_cobertura, resumen_cobertura(filas, datetime.fromtimestamp(actual, timezone.utc)))
    return "reconstruida"
