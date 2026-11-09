#!/usr/bin/env python3
"""
reconstruir_tarifas.py -- arma data/tarifas_cambios.csv (el historial de precios) a partir de los
snapshots crudos que existan. Se usa UNA VEZ, para recuperar el historial de los ultimos ~30 dias
antes de que la limpieza borre esos crudos; despues el propio sondeo lo mantiene al dia.

Que hace, paso a paso
---------------------
1. Busca los snapshots (snapshots/AAAA-MM-DD/HHMM.json.gz) y los ordena por fecha y hora.
2. Los recorre de mas viejo a mas nuevo. Para cada uno aplica LA MISMA funcion que usa el sondeo
   (sondear.cambios_de_tarifas): si un precio es nuevo o cambio respecto del ultimo conocido, anota
   una fila con la hora del snapshot (la hora UTC sale del nombre del archivo).
3. Si ya existe un historial, continua desde su ultima fila: es seguro correrlo mas de una vez
   (no duplica filas).

Uso
---
  python3 scripts/reconstruir_tarifas.py --snapshots RUTA/snapshots --data RUTA/data
        SIMULA: solo cuenta y muestra un resumen, no escribe nada.
  python3 scripts/reconstruir_tarifas.py --snapshots RUTA/snapshots --data RUTA/data --aplicar
        Escribe (agrega) data/tarifas_cambios.csv.
  Opcionales: --desde / --hasta (fechas-hora UTC ISO, p. ej. 2026-10-03T00:00:00+00:00).
"""
import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sondear  # noqa: E402


def momento_de(ruta: Path):
    """snapshots/2026-10-03/1159.json.gz -> '2026-10-03T11:59:00+00:00' (UTC), o None si el nombre no calza."""
    try:
        momento = datetime.strptime(ruta.parent.name + ruta.name[:4], "%Y-%m-%d%H%M")
    except ValueError:
        return None
    return momento.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshots", default=str(sondear.DIR_SNAPSHOTS), help="carpeta con los snapshots")
    ap.add_argument("--data", default=str(sondear.DIR_DATA), help="carpeta data/ donde vive tarifas_cambios.csv")
    ap.add_argument("--desde", help="solo snapshots desde esta fecha-hora UTC (ISO)")
    ap.add_argument("--hasta", help="solo snapshots hasta esta fecha-hora UTC (ISO)")
    ap.add_argument("--aplicar", action="store_true", help="escribe el historial (por defecto solo simula)")
    args = ap.parse_args()

    sondear.DIR_DATA = Path(args.data)          # archivo_tarifas() sigue a DIR_DATA
    ruta_historial = sondear.archivo_tarifas()
    ultimo = sondear.ultimo_precio_conocido(ruta_historial)
    ya_hecho_hasta = max((f.get("timestamp_deteccion", "") for f in sondear.leer_csv(ruta_historial)), default="")

    archivos = sorted(Path(args.snapshots).glob("*/*.json.gz"))
    if not archivos:
        sys.exit(f"No encontre snapshots en {args.snapshots}")

    filas, leidos, saltados, ilegibles = [], 0, 0, 0
    primero = ultimo_ts = None
    for i, ruta in enumerate(archivos, 1):
        ts = momento_de(ruta)
        if ts is None or (args.desde and ts < args.desde) or (args.hasta and ts > args.hasta) or ts <= ya_hecho_hasta:
            saltados += 1
            continue
        try:
            with gzip.open(ruta, "rt", encoding="utf-8") as f:
                datos = json.load(f)
            if not isinstance(datos, list):
                raise ValueError("no es una lista")
        except Exception as exc:                  # un snapshot danado no frena a los demas
            ilegibles += 1
            print(f"  AVISO: no pude leer {ruta.parent.name}/{ruta.name}: {exc}", file=sys.stderr)
            continue
        leidos += 1
        primero = primero or ts
        ultimo_ts = ts
        filas.extend(sondear.cambios_de_tarifas(ultimo, datos, ts))
        if i % 500 == 0:
            print(f"  ... {i}/{len(archivos)} archivos, {len(filas)} filas")

    bases = sum(1 for f in filas if f["precio_anterior"] == "" and f["precio_nuevo"] != "")
    cambios = sum(1 for f in filas if f["precio_anterior"] != "" and f["precio_nuevo"] != "")
    perdidas = sum(1 for f in filas if f["precio_nuevo"] == "")
    print(f"{'APLICANDO' if args.aplicar else 'SIMULACION (no se escribe nada)'}")
    print(f"Snapshots: {len(archivos)} encontrados · {leidos} leidos · {saltados} saltados · {ilegibles} ilegibles")
    if primero:
        print(f"Rango leido: {primero} -> {ultimo_ts}")
    print(f"Filas nuevas: {len(filas)} (lecturas base {bases} · cambios de precio {cambios} · precios que dejaron de informarse {perdidas})")
    if args.aplicar:
        sondear.agregar_csv(ruta_historial, sondear.COLUMNAS_TARIFAS, filas)
        print(f"Escrito: {ruta_historial}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
