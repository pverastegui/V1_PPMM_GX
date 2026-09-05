#!/usr/bin/env python3
"""
CHEQUEO DE SALUD DEL SONDEO

Corre aparte de sondear.py (workflow .github/workflows/chequeo_salud.yml,
cada 30 minutos) y hace dos cosas a la vez:

  1. DETECTAR FALLAS TEMPRANO: revisa data/corridas.csv (donde sea que
     viva -- repo publico o privado, ver SONDEAR_DIR_DATA en sondear.py) y
     confirma que el sondeo sigue corriendo de verdad y con exito. Si no --
     por ejemplo porque el token del repo privado vencio, o la API de
     cargadorespublicos.cl esta caida hace rato -- este script devuelve un
     codigo de salida distinto de cero. El workflow usa eso para marcarse a
     si mismo como "fallido", y GitHub manda un correo automatico cuando un
     workflow programado falla (si tienes las notificaciones de Actions
     activadas en https://github.com/settings/notifications -- ver
     LEEME.md). Sin este chequeo, una falla asi podria pasar semanas sin
     que nadie se de cuenta.

  2. EVITAR QUE GITHUB APAGUE EL SONDEO SOLO: GitHub desactiva automatica y
     silenciosamente los workflows programados ("schedule") de un repo
     PUBLICO si el repo no tiene ninguna actividad de git (commits) en 60
     dias. Con todos los datos ahora yendo al repo privado, el repo publico
     -- que es donde vive el propio disparador de sondear.py -- podria
     dejar de recibir commits del todo. Por eso este chequeo, corriendo en
     el repo publico, siempre escribe ESTADO.md con la hora de la revision
     y lo comitea: ese commit peridodico (cada 30 minutos) mantiene al repo
     publico "vivo" para GitHub, indefinidamente, sin exponer ningun dato
     real (ESTADO.md no tiene ninguna cifra de mercado, solo la hora de la
     ultima revision y si esta todo bien).

No revisa el crudo (snapshots/): ese se guarda solo a veces por diseno
(ver "SOBRE EL INTERVALO DE SONDEO" en sondear.py), asi que su ausencia en
una corrida puntual es normal y no significa una falla.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

from sondear import ARCHIVO_CORRIDAS, leer_csv  # noqa: E402

# Cuanto puede pasar sin ninguna corrida nueva (exitosa o no) antes de decir
# que el sondeo dejo de correr. El sondeo corre cada 1 minuto; se deja
# harto margen (45 min) porque GitHub a veces atrasa los workflows
# programados, sobre todo en cuentas gratuitas y en horas de mucha carga.
UMBRAL_SIN_CORRER_MIN = int(os.environ.get("CHEQUEO_UMBRAL_SIN_CORRER_MIN", "45"))

# Ventana en la que se espera ver AL MENOS una corrida exitosa. Tolera
# fallas puntuales (la API de cargadorespublicos.cl a veces falla y
# sondear.py reintenta solo) sin avisar por cada una -- solo avisa si
# TODAS las corridas de esta ventana fallaron.
VENTANA_EXITO_MIN = int(os.environ.get("CHEQUEO_VENTANA_EXITO_MIN", "60"))

ARCHIVO_ESTADO = RAIZ / "ESTADO.md"


def evaluar_salud(filas: list[dict], ahora: datetime) -> tuple[bool, str]:
    """Devuelve (sano, mensaje). No toca disco -- facil de probar."""
    if not filas:
        return True, "Todavia no hay ninguna corrida registrada en corridas.csv."

    ultima = filas[-1]
    try:
        ultima_ts = datetime.fromisoformat(ultima["timestamp"])
    except (KeyError, ValueError):
        return False, "La ultima fila de corridas.csv no tiene un timestamp valido."

    minutos_desde_ultima = (ahora - ultima_ts).total_seconds() / 60
    if minutos_desde_ultima > UMBRAL_SIN_CORRER_MIN:
        return False, (
            f"Sin corridas nuevas hace {minutos_desde_ultima:.0f} minutos "
            f"(deberia correr cada 1 minuto; umbral de aviso: {UMBRAL_SIN_CORRER_MIN} min). "
            f"Ultima corrida: {ultima['timestamp']}."
        )

    recientes = []
    for fila in reversed(filas):
        try:
            ts = datetime.fromisoformat(fila["timestamp"])
        except (KeyError, ValueError):
            continue
        if (ahora - ts).total_seconds() / 60 > VENTANA_EXITO_MIN:
            break
        recientes.append(fila)

    if recientes and not any(str(f.get("ok")) == "1" for f in recientes):
        return False, (
            f"Las ultimas {len(recientes)} corridas (ultimos {VENTANA_EXITO_MIN} min) "
            f"fallaron todas. Ultimo error: {recientes[0].get('error_tipo')}: "
            f"{recientes[0].get('error_mensaje')}"
        )

    return True, f"Ultima corrida: {ultima['timestamp']} (ok={ultima.get('ok')})."


def escribir_estado(sano: bool, mensaje: str, ahora_iso: str) -> None:
    ARCHIVO_ESTADO.write_text(
        "# Estado del sondeo\n\n"
        f"Ultima revision: {ahora_iso}\n\n"
        f"Estado: {'OK' if sano else 'FALLA'}\n\n"
        f"{mensaje}\n\n"
        "(Este archivo lo actualiza automaticamente "
        ".github/workflows/chequeo_salud.yml cada 30 minutos. No contiene "
        "ningun dato de mercado -- solo sirve para detectar rapido si el "
        "sondeo dejo de correr, y para que este repo nunca quede 60 dias "
        "sin actividad, algo que haria que GitHub apague el sondeo solo.)\n",
        encoding="utf-8",
    )


def main() -> int:
    ahora = datetime.now(timezone.utc)
    filas = leer_csv(ARCHIVO_CORRIDAS)
    sano, mensaje = evaluar_salud(filas, ahora)
    escribir_estado(sano, mensaje, ahora.isoformat(timespec="seconds"))
    print(("OK" if sano else "FALLA") + ": " + mensaje)
    return 0 if sano else 1


if __name__ == "__main__":
    sys.exit(main())
