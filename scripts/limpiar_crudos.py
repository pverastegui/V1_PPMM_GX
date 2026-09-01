#!/usr/bin/env python3
"""
Borra las carpetas de snapshots crudos mas viejas que DIAS_RETENCION.

Los CSVs derivados (eventos, catalogo, corridas) NO se tocan nunca: esos son
permanentes y son lo que realmente se analiza. El crudo es la red de seguridad
para poder reprocesar si se descubre un bug, y con 60 dias alcanza de sobra.

Lo corre GitHub Actions una vez al dia.
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIAS_RETENCION = 60

RAIZ = Path(__file__).resolve().parent.parent
DIR_SNAPSHOTS = RAIZ / "snapshots"


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
