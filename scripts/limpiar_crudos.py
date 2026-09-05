#!/usr/bin/env python3
"""
Borra las carpetas de snapshots crudos mas viejas que DIAS_RETENCION.

Los CSVs derivados (eventos, catalogo, corridas) NO se tocan nunca: esos son
permanentes y son lo que realmente se analiza. El crudo es la red de seguridad
para poder reprocesar si se descubre un bug, y con 60 dias alcanza de sobra.

Lo corre GitHub Actions una vez al dia.

Igual que en sondear.py, DIR_SNAPSHOTS se puede pisar con la variable de
entorno SONDEAR_DIR_SNAPSHOTS -- limpiar.yml la usa para limpiar tambien
el checkout del repo privado del crudo, si esta configurado (ver el
docstring de sondear.py, "SOBRE DONDE VIVE EL CRUDO", y LEEME.md).

Si el repo privado esta configurado, snapshots/ de ESTE repo publico
deberia quedar practicamente vacio la mayoria de los dias -- sondear.py ya
guarda el crudo directo alla. Este script sigue corriendo igual sobre este
repo, por las dudas (por ejemplo si algo fallo antes de que el repo
privado quedara configurado).

DIAS_RETENCION tambien se puede pisar con la variable de entorno
LIMPIAR_DIAS_RETENCION -- limpiar.yml usa esto para que el repo privado
tenga una retencion mas corta (30 dias) que el publico (60), que es el
valor por defecto si la variable no esta seteada.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIAS_RETENCION = int(os.environ.get("LIMPIAR_DIAS_RETENCION", "60"))

RAIZ = Path(__file__).resolve().parent.parent
_dir_snapshots_env = os.environ.get("SONDEAR_DIR_SNAPSHOTS")
DIR_SNAPSHOTS = Path(_dir_snapshots_env) if _dir_snapshots_env else (RAIZ / "snapshots")


def main() -> int:
    if not DIR_SNAPSHOTS.exists():
        print("No hay carpeta snapshots/ todavia, nada que limpiar.")
        return 0

    corte = (datetime.now(timezone.utc) - timedelta(days=DIAS_RETENCION)).date()
    borradas = 0
    mb_liberados = 0.0

    for carpeta in sorted(DIR_SNAPSHOTS.iterdir()):
        if not carpeta.is_dir():
            continue
        try:
            fecha = datetime.strptime(carpeta.name, "%Y-%m-%d").date()
        except ValueError:
            print(f"Se ignora '{carpeta.name}' (no tiene formato de fecha).")
            continue

        if fecha < corte:
            mb = sum(f.stat().st_size for f in carpeta.rglob("*")) / 1e6
            shutil.rmtree(carpeta)
            borradas += 1
            mb_liberados += mb
            print(f"Borrada {carpeta.name} ({mb:.1f} MB)")

    if borradas == 0:
        print(f"Nada mas viejo que {DIAS_RETENCION} dias (corte: {corte}).")
    else:
        print(f"Total: {borradas} carpetas, {mb_liberados:.1f} MB liberados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
