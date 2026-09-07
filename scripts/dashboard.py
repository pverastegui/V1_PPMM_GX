#!/usr/bin/env python3
"""
Genera dist/index.html: un dashboard estatico, en un solo archivo, leyendo los
CSVs que produce sondear.py. No necesita internet ni servidor: se abre con doble
click.

    python scripts/dashboard.py

Que muestra, y desde cuando sirve cada cosa:

  - Participacion de mercado por conectores y por kW instalados
    -> Sirve desde el PRIMER sondeo. Es una foto del parque instalado.

  - Transacciones por operador y por tramo de potencia
    -> Necesita tiempo. Cuenta las filas de eventos con estado_nuevo = OCUPADO,
       exactamente igual que la planilla.

  - Volumen estimado (kWh) y participacion por volumen
    -> transacciones x carga promedio del tramo. La carga promedio la defines tu
       en carga_promedio_tramo.csv (es un supuesto de negocio, no sale de la API).

  - Salud del pipeline: cuantos sondeos corrieron bien y cual fue el ultimo error.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DIR_DATA = RAIZ / "data"
DIR_EVENTOS = DIR_DATA / "eventos"
ARCHIVO_CATALOGO = DIR_DATA / "catalogo.csv"
ARCHIVO_CORRIDAS = DIR_DATA / "corridas.csv"
ARCHIVO_CARGA_PROMEDIO = RAIZ / "carga_promedio_tramo.csv"
SALIDA = RAIZ / "dist" / "index.html"

# Como se llama Copec en la columna operador_agrupado (para resaltarlo).
OPERADOR_FOCO = "Copec Voltex"

# Orden fijo de los tramos, de menor a mayor potencia.
ORDEN_TRAMOS = ["7", "(7-22]", "(22-50]", "(50-150]", "150", "desconocido"]


# ------------------------------------------------------------------- lectura

def leer_csv(ruta: Path) -> list[dict]:
    if not ruta.exists():
        return []
    with ruta.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def leer_todos_los_eventos() -> list[dict]:
    """Junta todos los eventos/AAAA-MM.csv en una sola lista."""
    if not DIR_EVENTOS.exists():
        return []
    filas = []
    for ruta in sorted(DIR_EVENTOS.glob("*.csv")):
        filas.extend(leer_csv(ruta))
    return filas


def num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ metricas

def parque_instalado(catalogo: list[dict]) -> list[dict]:
    """Participacion de mercado del parque instalado (solo conectores activos)."""
    por_op = defaultdict(lambda: {"conectores": 0, "kw": 0.0, "locations": set()})
    for fila in catalogo:
        if str(fila.get("activo")) != "1":
            continue
        op = fila.get("operador_agrupado") or fila.get("operator_name") or "Sin informar"
        d = por_op[op]
        d["conectores"] += 1
        d["kw"] += num(fila.get("max_electric_power"))
        d["locations"].add(fila.get("location_id"))

    filas = [{"op": op, "conectores": d["conectores"], "kw": d["kw"],
              "locations": len(d["locations"])} for op, d in por_op.items()]
    tot_con = sum(f["conectores"] for f in filas) or 1
    tot_kw = sum(f["kw"] for f in filas) or 1
    for f in filas:
        f["pct_conectores"] = 100 * f["conectores"] / tot_con
        f["pct_kw"] = 100 * f["kw"] / tot_kw
    return sorted(filas, key=lambda f: -f["conectores"])


def transacciones(eventos: list[dict], desde: datetime | None = None) -> list[dict]:
    """Una transaccion = un evento con estado_nuevo = OCUPADO.
    Devuelve filas (operador, tramo, n) mas los totales por operador."""
    conteo = defaultdict(int)
    for e in eventos:
        if e.get("estado_nuevo") != "OCUPADO":
            continue
        if desde is not None:
            try:
                if datetime.fromisoformat(e["timestamp_deteccion"]) < desde:
                    continue
            except (ValueError, KeyError):
                continue
        op = e.get("operador_agrupado") or e.get("operator_name") or "Sin informar"
        conteo[(op, e.get("tramo_potencia") or "desconocido")] += 1
    return [{"op": op, "tramo": tr, "n": n} for (op, tr), n in conteo.items()]


def volumen_estimado(trans: list[dict], carga: dict) -> list[dict]:
    """transacciones x kWh promedio del tramo, agrupado por operador."""
    por_op = defaultdict(lambda: {"transacciones": 0, "kwh": 0.0})
    for t in trans:
        d = por_op[t["op"]]
        d["transacciones"] += t["n"]
        d["kwh"] += t["n"] * carga.get(t["tramo"], 0.0)

    filas = [{"op": op, **d} for op, d in por_op.items()]
    tot = sum(f["kwh"] for f in filas) or 1
    for f in filas:
        f["pct_volumen"] = 100 * f["kwh"] / tot
    return sorted(filas, key=lambda f: -f["kwh"])


def serie_diaria(eventos: list[dict], ops: list[str], dias: int = 14) -> dict:
    """{fecha: {operador: n_transacciones}} para los ultimos N dias."""
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).date()
    por_dia = defaultdict(lambda: defaultdict(int))
    for e in eventos:
        if e.get("estado_nuevo") != "OCUPADO":
            continue
        op = e.get("operador_agrupado") or e.get("operator_name")
        if op not in ops:
            continue
        try:
            dia = datetime.fromisoformat(e["timestamp_deteccion"]).date()
        except (ValueError, KeyError):
            continue
        if dia >= corte:
            por_dia[dia.isoformat()][op] += 1
    return {k: dict(v) for k, v in por_dia.items()}


def salud(corridas: list[dict]) -> dict:
    if not corridas:
        return {"n": 0, "pct_ok": None, "ultima": None, "ultimo_error": None}
    desde = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    recientes = [c for c in corridas if (c.get("timestamp") or "") >= desde]
    ok = sum(1 for c in recientes if str(c.get("ok")) == "1")
    errores = [c for c in corridas if str(c.get("ok")) == "0"]
    return {
        "n": len(recientes),
        "pct_ok": (100 * ok / len(recientes)) if recientes else None,
        "ultima": corridas[-1].get("timestamp"),
        "ultimo_error": errores[-1] if errores else None,
    }


# --------------------------------------------------------------------- render

def esc(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def miles(x, dec=0) -> str:
    return f"{x:,.{dec}f}".replace(",", "@").replace(".", ",").replace("@", ".")


def barras(filas: list[dict], clave: str, unidad: str, top=10, ancho=720) -> str:
    """Barras horizontales. Todas del mismo color (es una sola magnitud);
    la de Copec resaltada, que es la comparacion que interesa."""
    filas = [f for f in filas if f.get(clave)][:top]
    if not filas:
        return '<p class="vacio">Todavia no hay datos para este grafico.</p>'
    maximo = max(f[clave] for f in filas) or 1
    alto_fila, etiqueta_w, margen_d = 34, 190, 90
    area = ancho - etiqueta_w - margen_d
    alto = alto_fila * len(filas) + 10

    piezas = []
    for i, f in enumerate(filas):
        y = i * alto_fila + 6
        w = max(2, f[clave] / maximo * area)
        foco = OPERADOR_FOCO.upper() in str(f["op"]).upper()
        color = "var(--serie-2)" if foco else "var(--serie-1)"
        valor = miles(f[clave])
        piezas.append(f'''
      <g tabindex="0"><title>{esc(f["op"])}: {valor} {esc(unidad)}</title>
        <text x="{etiqueta_w - 8}" y="{y + 16}" text-anchor="end" class="et">{esc(f["op"])}</text>
        <rect x="{etiqueta_w}" y="{y}" width="{w:.1f}" height="22" rx="4" fill="{color}"/>
        <text x="{etiqueta_w + w + 8}" y="{y + 16}" class="val">{valor} {esc(unidad)}</text>
      </g>''')

    return (f'<svg viewBox="0 0 {ancho} {alto}" width="100%" height="{alto}" '
            f'role="img" aria-label="ranking por {esc(unidad)}">{"".join(piezas)}</svg>')


def lineas(serie: dict, ops: list[str], ancho=820, alto=250) -> str:
    dias = sorted(serie.keys())
    if len(dias) < 2:
        return ('<p class="vacio">Se necesitan al menos 2 dias de datos para ver '
                'la tendencia. Vuelve mañana.</p>')

    izq, der, arr, aba = 42, 115, 16, 28
    pw, ph = ancho - izq - der, alto - arr - aba
    maximo = max((max(serie[d].values(), default=0) for d in dias), default=1) or 1
    x = lambda i: izq + (i / max(1, len(dias) - 1)) * pw
    y = lambda v: arr + ph - (v / maximo) * ph

    grilla = []
    for g in range(5):
        gy = arr + ph * g / 4
        grilla.append(f'<line x1="{izq}" y1="{gy:.1f}" x2="{ancho-der}" y2="{gy:.1f}" class="grilla"/>')
        grilla.append(f'<text x="{izq-6}" y="{gy+3:.1f}" text-anchor="end" class="eje">{round(maximo*(4-g)/4)}</text>')

    trazos, puntos, etiquetas = [], [], []
    for i_op, op in enumerate(ops[:4]):
        color = f"var(--serie-{i_op + 1})"
        pts = [(x(i), y(serie[d].get(op, 0))) for i, d in enumerate(dias)]
        d_attr = " ".join(f'{"M" if i==0 else "L"}{px:.1f},{py:.1f}' for i, (px, py) in enumerate(pts))
        trazos.append(f'<path d="{d_attr}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round"/>')
        for i, (px, py) in enumerate(pts):
            puntos.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{color}">'
                          f'<title>{esc(op)} · {dias[i]}: {serie[dias[i]].get(op,0)}</title></circle>')
        etiquetas.append([pts[-1][1], pts[-1][0], color, op if len(op) <= 15 else op[:14] + "…"])

    etiquetas.sort(key=lambda r: r[0])
    for i in range(1, len(etiquetas)):
        if etiquetas[i][0] - etiquetas[i-1][0] < 13:
            etiquetas[i][0] = etiquetas[i-1][0] + 13
    txt = [f'<text x="{px+6:.1f}" y="{py+4:.1f}" class="eti-linea" fill="{c}">{esc(n)}</text>'
           for py, px, c, n in etiquetas]

    paso = max(1, len(dias) // 6)
    ticks = [f'<text x="{x(i):.1f}" y="{alto-6}" text-anchor="middle" class="eje">{d[5:]}</text>'
             for i, d in enumerate(dias) if i % paso == 0 or i == len(dias) - 1]

    return (f'<svg viewBox="0 0 {ancho} {alto}" width="100%" height="{alto}" role="img" '
            f'aria-label="transacciones diarias">{"".join(grilla+trazos+puntos+txt+ticks)}</svg>')


def tabla(filas: list[dict], cols: list[tuple[str, str]], dec: dict | None = None) -> str:
    if not filas:
        return ""
    dec = dec or {}
    th = "".join(f"<th>{esc(t)}</th>" for _, t in cols)
    tr = ""
    for f in filas:
        tds = ""
        for k, _ in cols:
            v = f.get(k, "")
            tds += f"<td>{miles(v, dec[k]) if k in dec and isinstance(v, (int, float)) else esc(v)}</td>"
        tr += f"<tr>{tds}</tr>"
    return f'<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>'


def render() -> str:
    catalogo = leer_csv(ARCHIVO_CATALOGO)
    eventos = leer_todos_los_eventos()
    corridas = leer_csv(ARCHIVO_CORRIDAS)
    carga = {f["tramo_potencia"]: num(f["kwh_promedio_por_sesion"])
             for f in leer_csv(ARCHIVO_CARGA_PROMEDIO)}

    parque = parque_instalado(catalogo)
    trans = transacciones(eventos)
    volumen = volumen_estimado(trans, carga)
    top_ops = [f["op"] for f in parque[:4]]
    serie = serie_diaria(eventos, top_ops)
    s = salud(corridas)

    foco = next((f for f in parque if OPERADOR_FOCO.upper() in f["op"].upper()), None)
    total_trans = sum(t["n"] for t in trans)
    total_kwh = sum(v["kwh"] for v in volumen)

    # Matriz operador x tramo (la tabla central de la planilla)
    ops_orden = [f["op"] for f in parque]
    matriz = defaultdict(dict)
    for t in trans:
        matriz[t["op"]][t["tramo"]] = t["n"]
    tramos_presentes = [tr for tr in ORDEN_TRAMOS if any(tr in m for m in matriz.values())]
    filas_matriz = []
    for op in ops_orden:
        if op in matriz:
            fila = {"op": op, "total": sum(matriz[op].values())}
            for tr in tramos_presentes:
                fila[tr] = matriz[op].get(tr, 0)
            filas_matriz.append(fila)
    filas_matriz.sort(key=lambda f: -f["total"])

    if s["pct_ok"] is None:
        badge_clase, badge_txt = "warn", "sin sondeos en 24h"
    else:
        badge_clase = "ok" if s["pct_ok"] >= 95 else ("warn" if s["pct_ok"] >= 50 else "mal")
        badge_txt = f"{s['pct_ok']:.0f}% OK · {s['n']} sondeos en 24h"

    err = ""
    if s["ultimo_error"]:
        e = s["ultimo_error"]
        err = (f'<p class="nota">Ultimo error registrado: <code>{esc(e.get("error_tipo"))}</code> '
               f'el {esc(e.get("timestamp"))} — {esc(str(e.get("error_mensaje"))[:160])}</p>')

    aviso_carga = ""
    if not carga:
        aviso_carga = ('<p class="nota">Falta <code>carga_promedio_tramo.csv</code>, '
                       'asi que el volumen no se puede estimar.</p>')

    generado = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Los diccionarios se arman ACA, no dentro de la f-string: las llaves dentro
    # de una expresion de f-string no se pueden escapar con {{ }}.
    tabla_parque = tabla(
        parque,
        [("op", "Operador"), ("locations", "Sitios"), ("conectores", "Conectores"),
         ("pct_conectores", "% conectores"), ("kw", "kW"), ("pct_kw", "% kW")],
        dec={"pct_conectores": 1, "pct_kw": 1, "kw": 0},
    )
    tabla_volumen = tabla(
        volumen,
        [("op", "Operador"), ("transacciones", "Transacciones"),
         ("kwh", "kWh estimados"), ("pct_volumen", "% del mercado")],
        dec={"kwh": 0, "pct_volumen": 1},
    )
    tabla_matriz = tabla(
        filas_matriz,
        [("op", "Operador")] + [(tr, tr) for tr in tramos_presentes] + [("total", "Total")],
    ) or '<p class="vacio">Todavia no hay transacciones registradas.</p>'

    barras_transacciones = barras(
        [{"op": v["op"], "transacciones": v["transacciones"]} for v in volumen],
        "transacciones", "transacciones",
    )
    leyenda_lineas = "".join(
        f'<span><span class="sw" style="background:var(--serie-{i+1})"></span>{esc(o)}</span>'
        for i, o in enumerate(top_ops)
    )

    return f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monitor de carga publica — Chile</title>
<style>
  :root {{
    color-scheme: light;
    --fondo: #f9f9f7; --tarjeta: #fcfcfb;
    --tinta: #0b0b0b; --tinta-2: #52514e; --tinta-3: #898781;
    --grilla: #e1e0d9; --borde: rgba(11,11,11,.10);
    --serie-1: #2a78d6; --serie-2: #eb6834; --serie-3: #1baf7a; --serie-4: #eda100;
    --ok: #0ca30c; --alerta: #fab219; --critico: #d03b3b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --fondo: #0d0d0d; --tarjeta: #1a1a19;
      --tinta: #fff; --tinta-2: #c3c2b7; --tinta-3: #898781;
      --grilla: #2c2c2a; --borde: rgba(255,255,255,.10);
      --serie-1: #3987e5; --serie-2: #d95926; --serie-3: #199e70; --serie-4: #c98500;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--fondo); color:var(--tinta);
    font-family: system-ui,-apple-system,"Segoe UI",sans-serif; }}
  .wrap {{ max-width: 1000px; margin:0 auto; padding: 28px 20px 60px; }}
  h1 {{ font-size:1.4rem; margin:0 0 4px; }}
  .sub {{ color:var(--tinta-2); font-size:.88rem; margin:0 0 26px; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(175px,1fr)); gap:12px; margin-bottom:26px; }}
  .kpi {{ background:var(--tarjeta); border:1px solid var(--borde); border-radius:10px; padding:14px 16px; }}
  .kpi .t {{ font-size:.76rem; color:var(--tinta-2); margin-bottom:6px; }}
  .kpi .v {{ font-size:1.55rem; font-weight:600; }}
  .kpi.foco .v {{ color:var(--serie-2); }}
  section {{ background:var(--tarjeta); border:1px solid var(--borde); border-radius:12px;
    padding:18px 20px; margin-bottom:18px; overflow-x:auto; }}
  h2 {{ font-size:1rem; margin:0 0 4px; }}
  .desc {{ font-size:.82rem; color:var(--tinta-2); margin:0 0 14px; }}
  .vacio {{ color:var(--tinta-3); font-size:.85rem; font-style:italic; }}
  .nota {{ color:var(--tinta-3); font-size:.8rem; }}
  .et {{ font-size:12px; fill:var(--tinta-2); }}
  .val {{ font-size:12px; fill:var(--tinta); font-variant-numeric:tabular-nums; }}
  .grilla {{ stroke:var(--grilla); stroke-width:1; }}
  .eje {{ font-size:10px; fill:var(--tinta-3); }}
  .eti-linea {{ font-size:11px; font-weight:600; }}
  g:hover rect, g:focus rect {{ opacity:.82; }}
  .leyenda {{ display:flex; gap:15px; flex-wrap:wrap; margin-top:10px; font-size:.8rem; color:var(--tinta-2); }}
  .sw {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:middle; }}
  table {{ width:100%; border-collapse:collapse; font-size:.82rem; margin-top:8px; }}
  th,td {{ text-align:left; padding:5px 8px; border-bottom:1px solid var(--grilla);
    font-variant-numeric:tabular-nums; }}
  th {{ color:var(--tinta-2); font-weight:600; }}
  td:not(:first-child), th:not(:first-child) {{ text-align:right; }}
  details {{ margin-top:10px; }} summary {{ cursor:pointer; font-size:.8rem; color:var(--tinta-2); }}
  .badge {{ display:inline-block; padding:2px 9px; border-radius:999px; font-size:.75rem; font-weight:600; }}
  .badge.ok {{ background:color-mix(in srgb,var(--ok) 18%,transparent); color:var(--ok); }}
  .badge.warn {{ background:color-mix(in srgb,var(--alerta) 25%,transparent); color:#9a6b00; }}
  .badge.mal {{ background:color-mix(in srgb,var(--critico) 18%,transparent); color:var(--critico); }}
  footer {{ color:var(--tinta-3); font-size:.78rem; text-align:center; margin-top:28px; }}
</style>
</head>
<body><div class="wrap">

  <h1>Monitor de carga publica — Chile</h1>
  <p class="sub">Fuente: cargadorespublicos.cl/api/data (plataforma SEC) · generado {esc(generado)}</p>

  <div class="kpis">
    <div class="kpi"><div class="t">Conectores activos</div>
      <div class="v">{miles(sum(f["conectores"] for f in parque))}</div></div>
    <div class="kpi foco"><div class="t">{esc(OPERADOR_FOCO)} · % conectores</div>
      <div class="v">{(foco["pct_conectores"] if foco else 0):.1f}%</div></div>
    <div class="kpi foco"><div class="t">{esc(OPERADOR_FOCO)} · % kW instalados</div>
      <div class="v">{(foco["pct_kw"] if foco else 0):.1f}%</div></div>
    <div class="kpi"><div class="t">Transacciones acumuladas</div>
      <div class="v">{miles(total_trans)}</div></div>
    <div class="kpi"><div class="t">Volumen estimado (MWh)</div>
      <div class="v">{miles(total_kwh/1000, 1)}</div></div>
  </div>

  <section>
    <h2>Parque instalado — conectores</h2>
    <p class="desc">Foto del mercado hoy. Esto ya es confiable desde el primer sondeo.</p>
    {barras(parque, "conectores", "conectores")}
    <div class="leyenda">
      <span><span class="sw" style="background:var(--serie-1)"></span>Otros operadores</span>
      <span><span class="sw" style="background:var(--serie-2)"></span>{esc(OPERADOR_FOCO)}</span>
    </div>
    <details><summary>Ver tabla</summary>
      {tabla_parque}
    </details>
  </section>

  <section>
    <h2>Parque instalado — potencia (kW)</h2>
    <p class="desc">Pondera por capacidad, no por cantidad de puntos.</p>
    {barras(parque, "kw", "kW")}
  </section>

  <section>
    <h2>Transacciones acumuladas por operador</h2>
    <p class="desc">Cada transaccion es un cambio de estado a OCUPADO — el mismo
      criterio que usa la planilla. Se va llenando con el tiempo.</p>
    {barras_transacciones}
  </section>

  <section>
    <h2>Transacciones por operador y tramo de potencia</h2>
    <p class="desc">La tabla central: es lo mismo que la matriz CPO x Tramo de la planilla.</p>
    {tabla_matriz}
  </section>

  <section>
    <h2>Volumen estimado y participacion de mercado</h2>
    <p class="desc">transacciones x kWh promedio del tramo (segun
      <code>carga_promedio_tramo.csv</code>, que es tu supuesto de negocio).</p>
    {aviso_carga}
    {barras(volumen, "kwh", "kWh")}
    <details><summary>Ver tabla</summary>
      {tabla_volumen}
    </details>
  </section>

  <section>
    <h2>Transacciones diarias — top 4 operadores</h2>
    {lineas(serie, top_ops)}
    <div class="leyenda">{leyenda_lineas}</div>
  </section>

  <section>
    <h2>Salud del pipeline <span class="badge {badge_clase}">{esc(badge_txt)}</span></h2>
    <p class="desc">Ultimo sondeo: {esc(s["ultima"] or "todavia ninguno")}</p>
    {err}
  </section>

  <footer>Generado por scripts/dashboard.py · el filtro institucion_privada NO se aplica</footer>
</div></body></html>'''


def main() -> int:
    if not ARCHIVO_CATALOGO.exists():
        print("Todavia no existe data/catalogo.csv — corre primero: python scripts/sondear.py",
              file=sys.stderr)
    html = render()
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(html, encoding="utf-8")
    print(f"Dashboard escrito en {SALIDA} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
