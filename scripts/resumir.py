#!/usr/bin/env python3
"""
Produce dos CSVs chicos y ya agregados, pensados para que el visor los descargue
rapido desde el navegador (en vez de bajar los eventos completos, que crecen a
decenas de MB):

  data/resumen_diario.csv   vista, fecha, empresa, tramo_potencia, power_type, transacciones
  data/resumen_parque.csv   vista, empresa, power_type, sitios, conectores, kw
  data/cobertura_diaria.csv fecha, hueco_max_min, min_sin_cobertura, ultima_corrida
                            (dias en que el SONDEO estuvo caido: ver resumen_cobertura)

La columna "vista" dice COMO se atribuyo el conector, y hay una fila por cada
una de las tres formas (ver el docstring de sondear.py):
  opc    -> quien opera el punto de carga (la definicion habitual de "red")
  owner  -> de quien es la instalacion (el campo mas completo de los tres)
  pse    -> quien le vende la carga al usuario final

La columna "power_type" dice si el conector es AC o DC. Por cada combinacion
de las demas columnas hay TRES filas: una con power_type="" (el total, AC+DC
juntos) y una para cada tipo por separado. La fila en blanco no es la suma de
las otras dos calculada por el visor -- se calcula aca porque "sitios" es un
CONTEO DE UBICACIONES DISTINTAS, y una ubicacion con cargadores de ambos tipos
se contaria dos veces si el total fuera simplemente AC + DC.

Una transaccion = un evento con estado_nuevo = OCUPADO (el mismo criterio de la
planilla).

--- POR QUE LA FECHA ES EN HORA DE CHILE Y NO UTC ---
Los eventos se guardan en UTC. Pero cuando compares las sesiones diarias contra
los datos internos de Voltex, esos van a estar en hora de Chile. Un dia UTC no es
un dia chileno: entre las 20:00 y las 24:00 de Chile ya es el dia siguiente en
UTC, asi que agrupar por dia UTC te movia ~4 horas de sesiones al dia equivocado
y los totales no iban a calzar nunca. Por eso aca se convierte a
America/Santiago antes de sacar la fecha.
"""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ_CHILE = ZoneInfo("America/Santiago")
except Exception:  # pragma: no cover - si falta la base de datos de zonas
    TZ_CHILE = None

RAIZ = Path(__file__).resolve().parent.parent
DIR_DATA = Path(os.environ.get("SONDEAR_DIR_DATA", str(RAIZ / "data")))
DIR_EVENTOS = DIR_DATA / "eventos"
ARCHIVO_CATALOGO = DIR_DATA / "catalogo.csv"
SALIDA_DIARIO = DIR_DATA / "resumen_diario.csv"
SALIDA_PARQUE = DIR_DATA / "resumen_parque.csv"
ARCHIVO_CORRIDAS = DIR_DATA / "corridas.csv"
SALIDA_COBERTURA = DIR_DATA / "cobertura_diaria.csv"


def num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def leer_csv(ruta: Path) -> list[dict]:
    if not ruta.exists():
        return []
    with ruta.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def fecha_chile(ts: str) -> str | None:
    """'2026-09-01T23:30:00+00:00' -> '2026-09-01' en hora de Chile."""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if TZ_CHILE is not None:
        dt = dt.astimezone(TZ_CHILE)
    return dt.date().isoformat()


# columna del CSV donde vive cada vista, y como se llama en el resumen
VISTAS = {"opc": "operador_agrupado", "owner": "owner_agrupado", "pse": "pse_agrupado"}


def resumen_diario() -> list[dict]:
    conteo = defaultdict(int)
    if DIR_EVENTOS.exists():
        for ruta in sorted(DIR_EVENTOS.glob("*.csv")):
            for e in leer_csv(ruta):
                if e.get("estado_nuevo") != "OCUPADO":
                    continue
                # Las primeras lecturas NO son transacciones: es la primera vez
                # que el script ve ese conector, no significa que alguien enchufo.
                if e.get("estado_anterior") == "PRIMERA_LECTURA":
                    continue
                fecha = fecha_chile(e.get("timestamp_deteccion"))
                if not fecha:
                    continue
                tramo = e.get("tramo_potencia") or "desconocido"
                tipo = e.get("power_type") or "desconocido"
                for vista, columna in VISTAS.items():
                    # Los eventos viejos (anteriores a las tres vistas) no traen
                    # owner ni pse: en ese caso se cae a la vista OPC.
                    empresa = (e.get(columna) or e.get("operador_agrupado")
                               or e.get("operator_name") or "Sin informar")
                    # fila en blanco ("") = total AC+DC, ademas de la fila del tipo
                    for power_type in ("", tipo):
                        conteo[(vista, fecha, empresa, tramo, power_type)] += 1

    filas = [{"vista": v, "fecha": f, "empresa": o, "tramo_potencia": t,
              "power_type": tp, "transacciones": n}
             for (v, f, o, t, tp), n in conteo.items()]
    filas.sort(key=lambda r: (r["vista"], r["fecha"], -r["transacciones"]))
    return filas


def resumen_parque() -> list[dict]:
    por_grupo = defaultdict(lambda: {"sitios": set(), "conectores": 0, "kw": 0.0})
    for fila in leer_csv(ARCHIVO_CATALOGO):
        if str(fila.get("activo")) != "1":
            continue
        tipo = fila.get("power_type") or "desconocido"
        for vista, columna in VISTAS.items():
            empresa = (fila.get(columna) or fila.get("operador_agrupado")
                       or fila.get("operator_name") or "Sin informar")
            # fila en blanco ("") = total AC+DC, ademas de la fila del tipo
            for power_type in ("", tipo):
                d = por_grupo[(vista, empresa, power_type)]
                d["sitios"].add(fila.get("location_id"))
                d["conectores"] += 1
                d["kw"] += num(fila.get("max_electric_power"))

    filas = [{"vista": v, "empresa": e, "power_type": tp, "sitios": len(d["sitios"]),
              "conectores": d["conectores"], "kw": round(d["kw"])}
             for (v, e, tp), d in por_grupo.items()]
    filas.sort(key=lambda r: (r["vista"], r["power_type"], -r["conectores"]))
    return filas


# Un hueco entre dos corridas exitosas menor a esto es normal (el sondeo corre cada 1-5 min) y no se registra.
HUECO_MIN_REGISTRO_MIN = 15
_cache_fecha_hora: dict[int, str] = {}


def _fecha_de_hora_utc(k: int) -> str:
    """Hora UTC absoluta k (segundos / 3600) -> fecha en hora de Chile. Chile siempre tiene horas enteras de
    diferencia con UTC, asi que la fecha de cualquier instante depende solo de su hora UTC."""
    f = _cache_fecha_hora.get(k)
    if f is None:
        dt = datetime.fromtimestamp(k * 3600, timezone.utc)
        f = _cache_fecha_hora[k] = (dt.astimezone(TZ_CHILE) if TZ_CHILE is not None else dt).date().isoformat()
    return f


def resumen_cobertura(filas_corridas: list[dict]) -> list[dict]:
    """Dias (fecha en hora de Chile) en que el SONDEO tuvo huecos entre corridas, para que el visor pueda excluir los
    dias con caidas sin bajar corridas.csv completo (crece una fila por corrida: ~1.400 al dia).

    Una "caida" es un hueco entre dos corridas EXITOSAS (ok = 1) seguidas. Se mide el tiempo seguido sin mirar y no la
    cantidad de corridas por dia, porque el ritmo del sondeo cambio (cada 5 min hasta el 6-nov-2026, cada minuto desde el 7).

    Por cada dia que toca un hueco de HUECO_MIN_REGISTRO_MIN minutos o mas se guarda:
      hueco_max_min      el hueco COMPLETO mas largo que toca ese dia (un hueco de 70 min que cruza la medianoche, 50 + 20,
                         es de 70 min para los dos dias), incluido el dia en que el sondeo se reanuda aunque el hueco apenas
                         lo roce: ahi llega de golpe la rafaga de cambios acumulados.
      min_sin_cobertura  minutos de ese dia que caen dentro de huecos (solo informativo)
      ultima_corrida     la ultima corrida exitosa (igual en todas las filas): con ella el visor suma la caida que sigue
                         ocurriendo ahora (de la ultima corrida a la hora actual) sin bajar nada mas.
    Siempre incluye el dia de la ultima corrida (con 0 si no tuvo huecos), para que ultima_corrida nunca se pierda.
    """
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
    dias: dict[str, dict] = {}

    def dia(fecha: str) -> dict:
        return dias.setdefault(fecha, {"hueco_max_min": 0.0, "min_sin_cobertura": 0.0})

    for a, b in zip(marcas, marcas[1:]):
        hueco = (b - a) / 60
        if hueco < HUECO_MIN_REGISTRO_MIN:
            continue
        por_dia: dict[str, float] = defaultdict(float)
        k = int(a // 3600)
        while k * 3600 < b:
            por_dia[_fecha_de_hora_utc(k)] += (min(b, (k + 1) * 3600) - max(a, k * 3600)) / 60
            k += 1
        por_dia[_fecha_de_hora_utc(int(b // 3600))] += 0.0       # el dia en que se reanuda tambien cuenta
        for fecha, minutos in por_dia.items():
            d = dia(fecha)
            d["hueco_max_min"] = max(d["hueco_max_min"], hueco)
            d["min_sin_cobertura"] += minutos

    ultima = marcas[-1]
    dia(_fecha_de_hora_utc(int(ultima // 3600)))
    iso = datetime.fromtimestamp(ultima, timezone.utc).isoformat(timespec="seconds")
    return [{"fecha": f, "hueco_max_min": round(d["hueco_max_min"], 2),
             "min_sin_cobertura": round(d["min_sin_cobertura"], 2), "ultima_corrida": iso}
            for f, d in sorted(dias.items())]


def escribir(ruta: Path, columnas: list[str], filas: list[dict]) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columnas)
        w.writeheader()
        w.writerows(filas)


def main() -> int:
    if TZ_CHILE is None:
        print("AVISO: no se encontro la base de datos de zonas horarias, se usa UTC. "
              "Instala 'tzdata' (pip install tzdata) para que las fechas queden en "
              "hora de Chile.", file=sys.stderr)

    diario = resumen_diario()
    parque = resumen_parque()

    escribir(SALIDA_DIARIO,
             ["vista", "fecha", "empresa", "tramo_potencia", "power_type", "transacciones"], diario)
    escribir(SALIDA_PARQUE,
             ["vista", "empresa", "power_type", "sitios", "conectores", "kw"], parque)
    cobertura = resumen_cobertura(leer_csv(ARCHIVO_CORRIDAS))
    escribir(SALIDA_COBERTURA, ["fecha", "hueco_max_min", "min_sin_cobertura", "ultima_corrida"], cobertura)

    # Los totales de abajo usan solo la fila power_type="" (el total AC+DC) para
    # no contar cada transaccion/conector tres veces (total + AC + DC).
    dias = len({r["fecha"] for r in diario})
    total_opc = sum(r["transacciones"] for r in diario if r["vista"] == "opc" and r["power_type"] == "")
    print(f"resumen_diario.csv: {len(diario)} filas ({dias} dias, {total_opc} transacciones por vista)")
    for v in VISTAS:
        n = sum(r["conectores"] for r in parque if r["vista"] == v and r["power_type"] == "")
        emp = len([r for r in parque if r["vista"] == v and r["power_type"] == ""])
        print(f"  vista {v:6}: {emp:3} empresas, {n} conectores activos")
    con_caida = [r["fecha"] for r in cobertura if r["hueco_max_min"] >= 60]
    print(f"cobertura_diaria.csv: {len(cobertura)} dias con huecos o ultima corrida; "
          f"dias con caidas de 60 min o mas: {', '.join(con_caida) or 'ninguno'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
