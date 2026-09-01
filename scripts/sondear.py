#!/usr/bin/env python3
"""
SONDEO DE LA RED DE CARGADORES PUBLICOS DE CHILE
Fuente: https://cargadorespublicos.cl/api/data
(Plataforma de interoperabilidad SEC, Decreto Supremo N12 - Ministerio de Energia)

Este script corre UNA VEZ por ejecucion (lo dispara GitHub Actions cada 5 min).
Hace exactamente cuatro cosas, en este orden:

  1. Baja la API.
  2. GUARDA EL CRUDO tal cual vino, comprimido, en snapshots/<fecha>/<hora>.json.gz
     Esto pasa ANTES de procesar nada. Si el procesamiento tuviera un bug, el dato
     crudo ya esta a salvo y se puede reprocesar despues.
  3. Compara contra el estado anterior (data/catalogo.csv) y anota los cambios de
     estado en data/eventos/AAAA-MM.csv.
  4. Registra la corrida (exitosa o fallida) en data/corridas.csv.

Todo en CSV a proposito: se abren directo en Google Sheets o Excel, sin
herramientas extra.

--- ARCHIVOS QUE MANEJA ---

snapshots/AAAA-MM-DD/HHMM.json.gz
    El crudo, sin tocar. Un archivo por sondeo.

data/catalogo.csv
    Una fila por conector visto alguna vez. Se REESCRIBE completo cada corrida.
    Guarda el estado actual de cada conector, que es lo que permite detectar
    cambios en la corrida siguiente.

data/eventos/AAAA-MM.csv
    Log de cambios de estado, UN ARCHIVO POR MES. Solo se AGREGAN filas, nunca se
    borra nada. Se parte por mes para que cada archivo siga siendo chico y se
    pueda importar a Google Sheets sin problemas (un CSV de un año entero no
    entraria).
    Las columnas A-L son EXACTAMENTE las que espera la planilla de Paula, en el
    mismo orden, para que sus formulas sigan funcionando sin cambios:
      A timestamp_deteccion   G power_type
      B connector_id          H max_electric_power
      C operator_name         I standard
      D estado_anterior       J location_name
      E estado_nuevo          K operador_agrupado   <- antes era ARRAYFORMULA/BUSCARV
      F api_last_updated      L tramo_potencia      <- antes era ARRAYFORMULA anidada
    K y L ahora vienen calculadas desde aca, asi que en la planilla ya no hace
    falta mantener esas dos formulas (eran las mas fragiles).

data/corridas.csv
    Una fila por ejecucion, con ok=1/0 y el error si hubo. Sirve para saber si el
    pipeline se cayo en algun momento sin tener que revisar logs de GitHub.

mapeo_operadores.csv
    Tabla editable A MANO para agrupar razones sociales distintas del mismo
    operador (el equivalente a la hoja MapeoOperadores). Si un operador no esta
    en la tabla, se usa su nombre tal cual (no rompe nada).

--- LAS TRES FORMAS DE ATRIBUIR UN CONECTOR ---
La API identifica tres actores distintos por cada location, y dan numeros
distintos. Se guardan los tres para poder mirar el mercado de las tres formas:

  owner  -> de quien es la instalacion. Es el campo MAS COMPLETO: casi no tiene
            vacios (0,8% sin informar contra 12% del OPC).
  OPC    -> quien opera el punto de carga (la "red"). Es la definicion habitual
            de participacion de mercado, pero deja ~12% en "Sin Operador Informado".
  PSE    -> quien le vende la carga al usuario final. Es una LISTA (hoy ninguna
            location trae mas de uno; si llegaran varios se juntan con " + ").

--- NOTA SOBRE institucion_privada ---
El script viejo descartaba las locations con institucion_privada = true. Ese campo
indica si el SITIO pertenece a una institucion privada (un mall, una gasolinera),
no si el operador es publico o privado. Filtrar por el descartaba ~22% de las
locations, incluido 11% de las de Copec Voltex. Aca NO se filtra: la columna se
guarda en el catalogo por si algun dia la quieren usar, pero no descarta filas.
"""

from __future__ import annotations

import csv
import gzip
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

# ---------------------------------------------------------------- configuracion

API_URL = "https://cargadorespublicos.cl/api/data"
TIMEOUT_S = 30
REINTENTOS = 3
ESPERA_ENTRE_REINTENTOS_S = 5  # se duplica: 5s, 10s

# Horas que un conector puede estar ausente de la API antes de marcarlo retirado.
HORAS_GRACIA_RETIRO = 24

RAIZ = Path(__file__).resolve().parent.parent
DIR_SNAPSHOTS = RAIZ / "snapshots"
DIR_DATA = RAIZ / "data"
ARCHIVO_CATALOGO = DIR_DATA / "catalogo.csv"
DIR_EVENTOS = DIR_DATA / "eventos"          # un CSV por mes: eventos/2026-09.csv
ARCHIVO_CORRIDAS = DIR_DATA / "corridas.csv"
ARCHIVO_MAPEO = RAIZ / "mapeo_operadores.csv"

# Las 12 primeras columnas son las que usa la planilla (A-L). Lo que se agregue
# despues de la L no rompe nada; lo que se agregue ANTES si.
COLUMNAS_EVENTOS = [
    "timestamp_deteccion",   # A
    "connector_id",          # B
    "operator_name",         # C
    "estado_anterior",       # D
    "estado_nuevo",          # E
    "api_last_updated",      # F
    "power_type",            # G
    "max_electric_power",    # H
    "standard",              # I
    "location_name",         # J
    "operador_agrupado",     # K  (= la vista OPC, la de siempre)
    "tramo_potencia",        # L
    "commune",               # M
    "region",                # N
    # De la O en adelante: las otras dos formas de atribuir el conector.
    # Van DESPUES de la N para no correr de lugar nada de la planilla.
    "owner_agrupado",        # O
    "pse_agrupado",          # P
]

COLUMNAS_CATALOGO = [
    "connector_id", "evse_uid", "location_id", "location_name", "commune", "region",
    "operator_rut", "operator_name", "operador_agrupado", "standard", "power_type",
    "max_electric_power", "tramo_potencia", "parking_type", "institucion_privada",
    "uso_exclusivo", "estado_actual", "estado_desde", "api_last_updated",
    "primera_vez_visto", "ultima_vez_visto_api", "activo",
    # Las tres formas de atribuir un conector a una empresa (ver docstring):
    "owner_name", "owner_rut", "owner_agrupado",
    "pse_name", "pse_rut", "pse_agrupado",
]

COLUMNAS_CORRIDAS = [
    "timestamp", "ok", "http_status", "n_locations", "n_conectores",
    "n_eventos_nuevos", "archivo_crudo", "error_tipo", "error_mensaje",
]


# ------------------------------------------------------------------- utilidades

def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tramo_potencia(kw) -> str:
    """Misma clasificacion que la planilla. OJO: el tramo mas alto se escribe
    '150' a secas, NUNCA '>150' — Sheets interpreta el '>' como una condicion
    matematica y los conteos de ese tramo dan cero."""
    try:
        kw = float(kw)
    except (TypeError, ValueError):
        return "desconocido"
    if kw <= 8:
        return "7"
    if kw <= 22:
        return "(7-22]"
    if kw <= 50:
        return "(22-50]"
    if kw <= 150:
        return "(50-150]"
    return "150"


def cargar_mapeo_operadores() -> dict:
    """Lee mapeo_operadores.csv -> {nombre_original: nombre_agrupado}.
    Si el archivo no existe, devuelve vacio (y cada operador queda con su nombre)."""
    if not ARCHIVO_MAPEO.exists():
        return {}
    mapeo = {}
    with ARCHIVO_MAPEO.open(encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f):
            original = (fila.get("nombre_original") or "").strip()
            agrupado = (fila.get("nombre_agrupado") or "").strip()
            if original and agrupado:
                mapeo[original.upper()] = agrupado
    return mapeo


def archivo_eventos_del_mes(momento: datetime) -> Path:
    """data/eventos/2026-09.csv — un archivo por mes."""
    return DIR_EVENTOS / f"{momento.strftime('%Y-%m')}.csv"


def leer_csv(ruta: Path) -> list[dict]:
    if not ruta.exists():
        return []
    with ruta.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def escribir_csv(ruta: Path, columnas: list[str], filas: list[dict]) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columnas, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)


def agregar_csv(ruta: Path, columnas: list[str], filas: list[dict]) -> None:
    """Agrega filas al final. Escribe el encabezado solo si el archivo es nuevo."""
    if not filas:
        return
    ruta.parent.mkdir(parents=True, exist_ok=True)
    nuevo = not ruta.exists()
    with ruta.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columnas, extrasaction="ignore")
        if nuevo:
            w.writeheader()
        w.writerows(filas)


# ------------------------------------------------------------------ paso 1: API

def bajar_api() -> tuple[list | None, int | None, str | None, str | None]:
    """Devuelve (datos, http_status, error_tipo, error_mensaje).
    Nunca lanza excepcion: los errores vuelven como valores."""
    if requests is None:
        return None, None, "ImportError", "Falta 'requests'. Corre: pip install -r requirements.txt"

    ultimo_error = None
    ultimo_status = None

    for intento in range(1, REINTENTOS + 1):
        try:
            r = requests.get(API_URL, timeout=TIMEOUT_S)
            ultimo_status = r.status_code
            if r.status_code != 200:
                ultimo_error = RuntimeError(f"HTTP {r.status_code}")
            else:
                datos = r.json()
                if not isinstance(datos, list):
                    raise ValueError(f"Se esperaba una lista, llego {type(datos).__name__}")
                return datos, r.status_code, None, None
        except Exception as exc:  # red, timeout, JSON invalido, etc.
            ultimo_error = exc

        if intento < REINTENTOS:
            time.sleep(ESPERA_ENTRE_REINTENTOS_S * (2 ** (intento - 1)))

    return None, ultimo_status, type(ultimo_error).__name__, str(ultimo_error)[:400]


# --------------------------------------------------------- paso 2: guardar crudo

def guardar_crudo(datos: list, momento: datetime) -> Path:
    """Guarda el JSON tal cual, comprimido. Un archivo por sondeo.
    Esto se hace ANTES de procesar, para que el crudo este a salvo pase lo que pase."""
    carpeta = DIR_SNAPSHOTS / momento.strftime("%Y-%m-%d")
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / f"{momento.strftime('%H%M')}.json.gz"
    with gzip.open(ruta, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(datos, f, ensure_ascii=False)
    return ruta


# ------------------------------------------------- paso 3: detectar los cambios

def aplanar(datos: list, mapeo: dict) -> dict:
    """De la respuesta de la API saca un dict {connector_id: atributos planos}."""
    salida = {}
    for loc in datos:
        try:
            owner = loc.get("owner") or {}
            opc = loc.get("OPC") or {}
            pses = loc.get("PSEs") or []

            # --- VISTA 1: OPC, el operador del punto de carga (la de siempre) ---
            # Es el nombre oficial normalizado de la plataforma SEC. Si no viene
            # informado, se cae al nombre del dueño.
            operador = opc.get("normalized_name")
            if not operador or operador == "Sin Operador Informado":
                operador = owner.get("name") or "Sin Operador Informado"
            agrupado = mapeo.get(operador.upper(), operador)

            # --- VISTA 2: owner, de quien es la instalacion ---
            # Es el campo mas completo de los tres: casi no tiene vacios.
            owner_name = (owner.get("name") or "").strip() or "Sin dueño informado"
            owner_agrupado = mapeo.get(owner_name.upper(), owner_name)

            # --- VISTA 3: PSE, quien le vende la carga al usuario final ---
            # Es una LISTA. Hoy ninguna location trae mas de uno, pero si algun
            # dia llegan varios se juntan con " + " para no perder informacion
            # ni contar el mismo conector dos veces.
            nombres_pse = [(p.get("name") or "").strip() for p in pses if (p.get("name") or "").strip()]
            if nombres_pse:
                pse_name = " + ".join(sorted(set(nombres_pse)))
                pse_agrupado = (mapeo.get(nombres_pse[0].upper(), nombres_pse[0])
                                if len(set(nombres_pse)) == 1 else pse_name)
            else:
                pse_name = pse_agrupado = "Sin PSE informado"
            pse_rut = pses[0].get("RUT") if len(pses) == 1 else ""

            for evse in (loc.get("evses") or []):
                for con in (evse.get("connectors") or []):
                    cid = con.get("connector_id")
                    if cid is None:
                        continue
                    kw = con.get("max_electric_power")
                    salida[str(cid)] = {
                        "connector_id": cid,
                        "evse_uid": evse.get("evse_uid"),
                        "location_id": loc.get("location_id"),
                        "location_name": loc.get("name"),
                        "commune": loc.get("commune"),
                        "region": loc.get("region"),
                        "operator_rut": owner.get("RUT") or opc.get("RUT"),
                        "operator_name": operador,
                        "operador_agrupado": agrupado,
                        "standard": con.get("standard"),
                        "power_type": con.get("power_type"),
                        "max_electric_power": kw,
                        "tramo_potencia": tramo_potencia(kw),
                        "parking_type": loc.get("parking_type"),
                        "institucion_privada": 1 if loc.get("institucion_privada") else 0,
                        "uso_exclusivo": 1 if evse.get("uso_exclusivo") else 0,
                        "estado": (con.get("status") or "DESCONOCIDO").upper(),
                        "api_last_updated": evse.get("last_updated") or "",
                        "owner_name": owner_name,
                        "owner_rut": owner.get("RUT") or "",
                        "owner_agrupado": owner_agrupado,
                        "pse_name": pse_name,
                        "pse_rut": pse_rut,
                        "pse_agrupado": pse_agrupado,
                    }
        except (AttributeError, TypeError):
            # Una location con forma rara no debe tumbar el resto.
            continue
    return salida


def procesar(datos: list, momento_iso: str) -> tuple[list[dict], list[dict]]:
    """Compara la lectura nueva contra el catalogo guardado.
    Devuelve (catalogo_nuevo, eventos_nuevos)."""
    mapeo = cargar_mapeo_operadores()
    nuevo = aplanar(datos, mapeo)
    anterior = {f["connector_id"]: f for f in leer_csv(ARCHIVO_CATALOGO)}

    catalogo = []
    eventos = []
    momento = datetime.fromisoformat(momento_iso)

    for cid in set(nuevo) | set(anterior):
        actual = nuevo.get(cid)
        previo = anterior.get(cid)

        # --- caso 1: el conector vino en esta lectura ---
        if actual is not None:
            estado_anterior = previo["estado_actual"] if previo else ""
            estado_nuevo = actual["estado"]
            cambio = estado_anterior != estado_nuevo

            if cambio:
                eventos.append({
                    "timestamp_deteccion": momento_iso,
                    "connector_id": actual["connector_id"],
                    "operator_name": actual["operator_name"],
                    "estado_anterior": estado_anterior or "PRIMERA_LECTURA",
                    "estado_nuevo": estado_nuevo,
                    "api_last_updated": actual["api_last_updated"],
                    "power_type": actual["power_type"],
                    "max_electric_power": actual["max_electric_power"],
                    "standard": actual["standard"],
                    "location_name": actual["location_name"],
                    "operador_agrupado": actual["operador_agrupado"],
                    "tramo_potencia": actual["tramo_potencia"],
                    "commune": actual["commune"],
                    "region": actual["region"],
                    "owner_agrupado": actual["owner_agrupado"],
                    "pse_agrupado": actual["pse_agrupado"],
                })

            fila = {k: actual.get(k) for k in COLUMNAS_CATALOGO if k in actual}
            fila["estado_actual"] = estado_nuevo
            fila["estado_desde"] = momento_iso if cambio else (previo.get("estado_desde") if previo else momento_iso)
            fila["primera_vez_visto"] = previo.get("primera_vez_visto") if previo else momento_iso
            fila["ultima_vez_visto_api"] = momento_iso
            fila["activo"] = 1
            catalogo.append(fila)

        # --- caso 2: lo conociamos pero no vino ahora ---
        elif previo is not None:
            fila = dict(previo)
            if str(previo.get("activo")) == "1":
                try:
                    visto = datetime.fromisoformat(previo["ultima_vez_visto_api"])
                    ausente_h = (momento - visto).total_seconds() / 3600
                except (ValueError, KeyError, TypeError):
                    ausente_h = 0

                if ausente_h > HORAS_GRACIA_RETIRO:
                    eventos.append({
                        "timestamp_deteccion": momento_iso,
                        "connector_id": previo.get("connector_id"),
                        "operator_name": previo.get("operator_name"),
                        "estado_anterior": previo.get("estado_actual"),
                        "estado_nuevo": "RETIRADO_DE_API",
                        "api_last_updated": "",
                        "power_type": previo.get("power_type"),
                        "max_electric_power": previo.get("max_electric_power"),
                        "standard": previo.get("standard"),
                        "location_name": previo.get("location_name"),
                        "operador_agrupado": previo.get("operador_agrupado"),
                        "tramo_potencia": previo.get("tramo_potencia"),
                        "commune": previo.get("commune"),
                        "region": previo.get("region"),
                        "owner_agrupado": previo.get("owner_agrupado"),
                        "pse_agrupado": previo.get("pse_agrupado"),
                    })
                    fila["estado_actual"] = "RETIRADO_DE_API"
                    fila["estado_desde"] = momento_iso
                    fila["activo"] = 0
            catalogo.append(fila)

    catalogo.sort(key=lambda f: int(f.get("connector_id") or 0))
    return catalogo, eventos


# ------------------------------------------------------------------------ main

def main() -> int:
    momento = datetime.now(timezone.utc)
    momento_iso = momento.isoformat(timespec="seconds")

    datos, status, err_tipo, err_msg = bajar_api()

    if datos is None:
        agregar_csv(ARCHIVO_CORRIDAS, COLUMNAS_CORRIDAS, [{
            "timestamp": momento_iso, "ok": 0, "http_status": status,
            "error_tipo": err_tipo, "error_mensaje": err_msg,
        }])
        print(f"[{momento_iso}] FALLO: {err_tipo}: {err_msg}", file=sys.stderr)
        return 1

    # Paso 2 antes que nada: el crudo a salvo.
    ruta_crudo = guardar_crudo(datos, momento)
    print(f"Crudo guardado: {ruta_crudo.relative_to(RAIZ)} ({ruta_crudo.stat().st_size/1024:.0f} KB)")

    try:
        catalogo, eventos = procesar(datos, momento_iso)
        escribir_csv(ARCHIVO_CATALOGO, COLUMNAS_CATALOGO, catalogo)
        agregar_csv(archivo_eventos_del_mes(momento), COLUMNAS_EVENTOS, eventos)
        agregar_csv(ARCHIVO_CORRIDAS, COLUMNAS_CORRIDAS, [{
            "timestamp": momento_iso, "ok": 1, "http_status": status,
            "n_locations": len(datos), "n_conectores": len(catalogo),
            "n_eventos_nuevos": len(eventos),
            "archivo_crudo": str(ruta_crudo.relative_to(RAIZ)).replace("\\", "/"),
        }])
        print(f"OK: {len(datos)} locations, {len(catalogo)} conectores, {len(eventos)} eventos nuevos")
        return 0

    except Exception as exc:
        # El crudo ya esta guardado, asi que esto se puede reprocesar despues.
        agregar_csv(ARCHIVO_CORRIDAS, COLUMNAS_CORRIDAS, [{
            "timestamp": momento_iso, "ok": 0, "http_status": status,
            "archivo_crudo": str(ruta_crudo.relative_to(RAIZ)).replace("\\", "/"),
            "error_tipo": type(exc).__name__,
            "error_mensaje": f"{exc} | {traceback.format_exc()[-300:]}",
        }])
        print(f"[{momento_iso}] ERROR procesando (el crudo si quedo guardado): {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
