#!/usr/bin/env python3
"""
Produce dos CSVs chicos y ya agregados, pensados para que el visor los descargue
rapido desde el navegador (en vez de bajar los eventos completos, que crecen a
decenas de MB):

  data/resumen_diario.csv   fecha, operador_agrupado, tramo_potencia, transacciones
  data/resumen_parque.csv   operador_agrupado, sitios, conectores, kw

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
DIR_DATA = RAIZ / "data"
DIR_EVENTOS = DIR_DATA / "eventos"
ARCHIVO_CATALOGO = DIR_DATA / "catalogo.csv"
SALIDA_DIARIO = DIR_DATA / "resumen_diario.csv"
SALIDA_PARQUE = DIR_DATA / "resumen_parque.csv"


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
                op = e.get("operador_agrupado") or e.get("operator_name") or "Sin informar"
                tramo = e.get("tramo_potencia") or "desconocido"
                conteo[(fecha, op, tramo)] += 1

    filas = [{"fecha": f, "operador_agrupado": o, "tramo_potencia": t, "transacciones": n}
             for (f, o, t), n in conteo.items()]
    filas.sort(key=lambda r: (r["fecha"], -r["transacciones"]))
    return filas


def resumen_parque() -> list[dict]:
    por_op = defaultdict(lambda: {"sitios": set(), "conectores": 0, "kw": 0.0})
    for fila in leer_csv(ARCHIVO_CATALOGO):
        if str(fila.get("activo")) != "1":
            continue
        op = fila.get("operador_agrupado") or fila.get("operator_name") or "Sin informar"
        d = por_op[op]
        d["sitios"].add(fila.get("location_id"))
        d["conectores"] += 1
        d["kw"] += num(fila.get("max_electric_power"))

    filas = [{"operador_agrupado": op, "sitios": len(d["sitios"]),
              "conectores": d["conectores"], "kw": round(d["kw"])}
             for op, d in por_op.items()]
    filas.sort(key=lambda r: -r["conectores"])
    return filas


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

    escribir(SALIDA_DIARIO, ["fecha", "operador_agrupado", "tramo_potencia", "transacciones"], diario)
    escribir(SALIDA_PARQUE, ["operador_agrupado", "sitios", "conectores", "kw"], parque)

    dias = len({r["fecha"] for r in diario})
    total = sum(r["transacciones"] for r in diario)
    print(f"resumen_diario.csv: {len(diario)} filas, {dias} dias, {total} transacciones")
    print(f"resumen_parque.csv: {len(parque)} operadores, "
          f"{sum(r['conectores'] for r in parque)} conectores activos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
