#!/usr/bin/env python3
"""
Genera dist/index.html: un dashboard estatico y autosuficiente (sin backend)
a partir de data/cargadores.db. Pensado para ser publicado tal cual en
Cloudflare Pages / Netlify (o abierto localmente con doble click).

Metricas:
  - Participacion de mercado (locations, conectores, kW) por operador, sobre
    conectores activos.
  - Sesiones detectadas y kWh estimados por operador en una ventana movil.
  - Serie diaria de sesiones para los operadores con mas volumen (ultimos 14 dias).
  - Panel de salud del pipeline: exito de los ultimos sondeos, ultimo error.

Paleta y reglas de accesibilidad siguiendo la skill dataviz del equipo:
  - Barras nominales (identidad = nombre de operador): TODAS el mismo hue
    (slot 1, azul), excepto Copec Voltex resaltado en slot 2 (naranja) -
    2 categorias (foco vs resto), no un color distinto por operador.
  - Lineas de tendencia: hasta 4 series con los slots 1-4 (validado
    adjacente y con floor de vision normal en ambos modos), + "Otros" en gris.
  - Modo oscuro vive en las mismas variables CSS, no es un tema aparte.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from poller import DEFAULT_DB_PATH, get_db  # noqa: E402

OUT_PATH = Path(__file__).resolve().parent.parent / "dist" / "index.html"
FOCUS_OPERATOR = "COPEC VOLTEX"
SESSIONS_WINDOW_H = 24
TREND_DAYS = 14
TOP_N_TREND = 4

PALETTE = {
    "blue": ("#2a78d6", "#3987e5"),
    "orange": ("#eb6834", "#d95926"),
    "aqua": ("#1baf7a", "#199e70"),
    "yellow": ("#eda100", "#c98500"),
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch_market_share(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            operator_name AS op,
            COUNT(DISTINCT location_id) AS n_locations,
            COUNT(*) AS n_connectors,
            COALESCE(SUM(max_electric_power), 0) AS kw
        FROM connectors
        WHERE active = 1
        GROUP BY operator_name
        ORDER BY n_connectors DESC
        """
    ).fetchall()
    data = [dict(r) for r in rows]
    tot_loc = sum(r["n_locations"] for r in data) or 1
    tot_conn = sum(r["n_connectors"] for r in data) or 1
    tot_kw = sum(r["kw"] for r in data) or 1
    for r in data:
        r["pct_locations"] = 100 * r["n_locations"] / tot_loc
        r["pct_connectors"] = 100 * r["n_connectors"] / tot_conn
        r["pct_kw"] = 100 * r["kw"] / tot_kw
    return data


def fetch_sessions_window(conn: sqlite3.Connection, hours: int) -> list[dict]:
    since = (now_utc() - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT operator_name AS op,
               COUNT(*) AS n_sesiones,
               COALESCE(SUM(kwh_estimated), 0) AS kwh
        FROM sessions
        WHERE start_ts >= ? AND still_open = 0
        GROUP BY operator_name
        ORDER BY kwh DESC
        """,
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_daily_trend(conn: sqlite3.Connection, days: int, top_ops: list[str]) -> dict:
    since = (now_utc() - timedelta(days=days)).isoformat(timespec="seconds")
    placeholders = ",".join("?" for _ in top_ops)
    rows = conn.execute(
        f"""
        SELECT substr(start_ts, 1, 10) AS dia, operator_name AS op, COUNT(*) AS n
        FROM sessions
        WHERE start_ts >= ? AND still_open = 0 AND operator_name IN ({placeholders})
        GROUP BY dia, op
        ORDER BY dia
        """,
        (since, *top_ops),
    ).fetchall()
    by_day: dict[str, dict[str, int]] = {}
    for r in rows:
        by_day.setdefault(r["dia"], {})[r["op"]] = r["n"]
    return by_day


def fetch_health(conn: sqlite3.Connection) -> dict:
    last = conn.execute("SELECT * FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()
    since = (now_utc() - timedelta(hours=24)).isoformat(timespec="seconds")
    runs_24h = conn.execute("SELECT ok FROM poll_runs WHERE ts >= ?", (since,)).fetchall()
    n_total = len(runs_24h)
    n_ok = sum(1 for r in runs_24h if r["ok"])
    last_error = conn.execute(
        "SELECT * FROM poll_runs WHERE ok = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "last_run_ts": last["ts"] if last else None,
        "last_run_ok": bool(last["ok"]) if last else None,
        "pct_ok_24h": (100 * n_ok / n_total) if n_total else None,
        "n_runs_24h": n_total,
        "last_error": dict(last_error) if last_error else None,
    }


def esc(s) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def bar_chart_svg(data: list[dict], value_key: str, label_key: str, unit: str,
                   focus_name: str, width: int = 720, top_n: int = 10) -> str:
    rows = data[:top_n]
    if not rows:
        return '<p class="muted">Sin datos todavia.</p>'
    max_val = max(r[value_key] for r in rows) or 1
    row_h = 34
    height = row_h * len(rows) + 10
    label_w = 190
    bar_area = width - label_w - 70

    bars = []
    for i, r in enumerate(rows):
        y = i * row_h + 6
        w = max(2, (r[value_key] / max_val) * bar_area)
        is_focus = focus_name.upper() in (r[label_key] or "").upper()
        color_var = "var(--series-2)" if is_focus else "var(--series-1)"
        val_txt = f'{r[value_key]:,.0f}'.replace(",", ".")
        bars.append(f'''
    <g class="bar-row" tabindex="0">
      <title>{esc(r[label_key])}: {val_txt} {esc(unit)}</title>
      <text x="{label_w - 8}" y="{y + 17}" text-anchor="end" class="bar-label">{esc(r[label_key])}</text>
      <rect x="{label_w}" y="{y}" width="{w:.1f}" height="22" rx="4" fill="{color_var}"/>
      <text x="{label_w + w + 8}" y="{y + 17}" class="bar-value">{val_txt} {esc(unit)}</text>
    </g>''')

    return f'''<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img"
     aria-label="Ranking por {esc(value_key)}" class="chart">{"".join(bars)}</svg>'''


def line_chart_svg(by_day: dict, series_names: list[str], width: int = 760, height: int = 260) -> str:
    days = sorted(by_day.keys())
    if not days:
        return '<p class="muted">Sin datos todavia.</p>'

    pad_l, pad_r, pad_t, pad_b = 40, 110, 16, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    max_v = 1
    for d in days:
        for s in series_names:
            max_v = max(max_v, by_day[d].get(s, 0))

    def x_of(i):
        return pad_l + (i / max(1, len(days) - 1)) * plot_w

    def y_of(v):
        return pad_t + plot_h - (v / max_v) * plot_h

    paths = []
    dots = []
    end_labels = []  # (y, xml_fragment) - se reordena despues para evitar solapes
    for si, name in enumerate(series_names[:4]):
        color_var = f"var(--series-{si + 1})"
        pts = [(x_of(i), y_of(by_day[d].get(name, 0))) for i, d in enumerate(days)]
        path_d = " ".join(f'{"M" if i == 0 else "L"}{x:.1f},{y:.1f}' for i, (x, y) in enumerate(pts))
        paths.append(f'<path d="{path_d}" fill="none" stroke="{color_var}" stroke-width="2" stroke-linecap="round"/>')
        for i, (x, y) in enumerate(pts):
            v = by_day[days[i]].get(name, 0)
            dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color_var}"><title>{esc(name)} · {days[i]}: {v} sesiones</title></circle>')
        last_x, last_y = pts[-1]
        short_name = name if len(name) <= 14 else name[:13] + "…"
        end_labels.append([last_y, last_x, color_var, short_name])

    # Evitar que las etiquetas de fin de linea se solapen verticalmente.
    end_labels.sort(key=lambda r: r[0])
    min_gap = 13
    for i in range(1, len(end_labels)):
        if end_labels[i][0] - end_labels[i - 1][0] < min_gap:
            end_labels[i][0] = end_labels[i - 1][0] + min_gap
    labels = [
        f'<text x="{x + 6:.1f}" y="{y + 4:.1f}" class="line-label" fill="{color}">{esc(name)}</text>'
        for y, x, color, name in end_labels
    ]

    grid = []
    for gy in range(0, 5):
        y = pad_t + plot_h * gy / 4
        val = round(max_v * (4 - gy) / 4)
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="gridline"/>')
        grid.append(f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" class="axis-label">{val}</text>')

    x_ticks = []
    step = max(1, len(days) // 6)
    for i, d in enumerate(days):
        if i % step == 0 or i == len(days) - 1:
            x_ticks.append(f'<text x="{x_of(i):.1f}" y="{height - 6}" text-anchor="middle" class="axis-label">{d[5:]}</text>')

    return f'''<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img"
     aria-label="Sesiones diarias por operador" class="chart">
    {"".join(grid)}
    {"".join(paths)}
    {"".join(dots)}
    {"".join(labels)}
    {"".join(x_ticks)}
  </svg>'''


def table_view(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for _, h in columns)
    body = ""
    for r in rows:
        cells = "".join(f"<td>{esc(r.get(k, ''))}</td>" for k, _ in columns)
        body += f"<tr>{cells}</tr>"
    return f'<table class="data-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def render(conn: sqlite3.Connection) -> str:
    market = fetch_market_share(conn)
    sessions_24h = fetch_sessions_window(conn, SESSIONS_WINDOW_H)
    top_ops = [r["op"] for r in sorted(market, key=lambda r: -r["n_connectors"])[:TOP_N_TREND]]
    trend = fetch_daily_trend(conn, TREND_DAYS, top_ops)
    health = fetch_health(conn)

    focus_row = next((r for r in market if FOCUS_OPERATOR in (r["op"] or "").upper() or r["op"] == FOCUS_OPERATOR), None)
    focus_pct_conn = focus_row["pct_connectors"] if focus_row else 0
    focus_pct_kw = focus_row["pct_kw"] if focus_row else 0

    total_sesiones = sum(r["n_sesiones"] for r in sessions_24h)
    total_kwh = sum(r["kwh"] for r in sessions_24h)
    total_conectores_activos = sum(r["n_connectors"] for r in market)

    market_by_conn = sorted(market, key=lambda r: -r["n_connectors"])
    market_by_kw = sorted(market, key=lambda r: -r["kw"])
    sessions_sorted = sorted(sessions_24h, key=lambda r: -r["kwh"])

    # Cuando todavia no hay corridas en las ultimas 24h (base recien creada, o
    # pipeline detenido) pct_ok_24h viene None: se muestra "sin datos" en vez de
    # reventar al formatear.
    pct_ok = health["pct_ok_24h"]
    if pct_ok is None:
        health_badge = "warn"
        health_badge_txt = "sin sondeos en 24h"
    else:
        health_badge = "ok" if pct_ok >= 90 else ("warn" if pct_ok >= 50 else "bad")
        health_badge_txt = f"{pct_ok:.0f}% OK (24h)"
    last_run_txt = health["last_run_ts"] or "sin datos"
    error_html = ""
    if health["last_error"]:
        e = health["last_error"]
        error_html = f'''<p class="muted">Ultimo error: <code>{esc(e["error_type"])}</code> el {esc(e["ts"])} —
        {esc((e["error_message"] or "")[:180])}</p>'''

    generated_at = now_utc().strftime("%Y-%m-%d %H:%M UTC")

    return f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monitor de Cargadores Publicos — Chile</title>
<style>
  :root {{
    color-scheme: light;
    --surface-1: #fcfcfb;
    --surface-page: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --gridline: #e1e0d9;
    --baseline: #c3c2b7;
    --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6;
    --series-2: #eb6834;
    --series-3: #1baf7a;
    --series-4: #eda100;
    --good: #0ca30c;
    --warning: #fab219;
    --critical: #d03b3b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --surface-1: #1a1a19;
      --surface-page: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --gridline: #2c2c2a;
      --baseline: #383835;
      --border: rgba(255,255,255,0.10);
      --series-1: #3987e5;
      --series-2: #d95926;
      --series-3: #199e70;
      --series-4: #c98500;
      --good: #0ca30c;
      --warning: #fab219;
      --critical: #d03b3b;
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --surface-1: #1a1a19;
    --surface-page: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --gridline: #2c2c2a;
    --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --series-4: #c98500;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--surface-page); color: var(--text-primary);
          font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 28px 20px 60px; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 4px; }}
  .subtitle {{ color: var(--text-secondary); font-size: 0.9rem; margin: 0 0 24px; }}
  .muted {{ color: var(--text-muted); font-size: 0.85rem; }}
  .stat-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
               gap: 12px; margin-bottom: 28px; }}
  .stat-tile {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
                padding: 14px 16px; }}
  .stat-tile .label {{ font-size: 0.78rem; color: var(--text-secondary); margin-bottom: 6px; }}
  .stat-tile .value {{ font-size: 1.6rem; font-weight: 600; font-variant-numeric: proportional-nums; }}
  .stat-tile.focus .value {{ color: var(--series-2); }}
  section {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
             padding: 18px 20px; margin-bottom: 20px; }}
  section h2 {{ font-size: 1rem; margin: 0 0 4px; }}
  section .desc {{ font-size: 0.82rem; color: var(--text-secondary); margin: 0 0 14px; }}
  .chart {{ display: block; }}
  .bar-label {{ font-size: 12px; fill: var(--text-secondary); }}
  .bar-value {{ font-size: 12px; fill: var(--text-primary); font-variant-numeric: tabular-nums; }}
  .bar-row rect {{ transition: opacity 0.1s; }}
  .bar-row:hover rect, .bar-row:focus rect {{ opacity: 0.8; }}
  .gridline {{ stroke: var(--gridline); stroke-width: 1; }}
  .axis-label {{ font-size: 10px; fill: var(--text-muted); }}
  .line-label {{ font-size: 11px; font-weight: 600; }}
  .legend {{ display: flex; gap: 16px; flex-wrap: wrap; margin-top: 10px; font-size: 0.8rem;
             color: var(--text-secondary); }}
  .legend .swatch {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px;
                      margin-right: 5px; vertical-align: middle; }}
  .badge {{ display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 0.75rem;
            font-weight: 600; }}
  .badge.ok {{ background: color-mix(in srgb, var(--good) 18%, transparent); color: var(--good); }}
  .badge.warn {{ background: color-mix(in srgb, var(--warning) 25%, transparent); color: #9a6b00; }}
  .badge.bad {{ background: color-mix(in srgb, var(--critical) 18%, transparent); color: var(--critical); }}
  details {{ margin-top: 10px; }}
  summary {{ cursor: pointer; font-size: 0.8rem; color: var(--text-secondary); }}
  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; margin-top: 8px; }}
  table.data-table th, table.data-table td {{ text-align: left; padding: 5px 8px;
    border-bottom: 1px solid var(--gridline); font-variant-numeric: tabular-nums; }}
  table.data-table th {{ color: var(--text-secondary); font-weight: 600; }}
  footer {{ color: var(--text-muted); font-size: 0.78rem; text-align: center; margin-top: 30px; }}
</style>
</head>
<body>
<div class="viz-root wrap">
  <h1>Monitor de Cargadores Publicos — Chile</h1>
  <p class="subtitle">Fuente: cargadorespublicos.cl/api/data (plataforma SEC) · generado {esc(generated_at)}</p>

  <div class="stat-row">
    <div class="stat-tile"><div class="label">Conectores activos monitoreados</div>
      <div class="value">{total_conectores_activos:,}</div></div>
    <div class="stat-tile focus"><div class="label">Copec Voltex · % de conectores</div>
      <div class="value">{focus_pct_conn:.1f}%</div></div>
    <div class="stat-tile focus"><div class="label">Copec Voltex · % de kW instalados</div>
      <div class="value">{focus_pct_kw:.1f}%</div></div>
    <div class="stat-tile"><div class="label">Sesiones detectadas ({SESSIONS_WINDOW_H}h)</div>
      <div class="value">{total_sesiones:,}</div></div>
    <div class="stat-tile"><div class="label">kWh estimados ({SESSIONS_WINDOW_H}h)</div>
      <div class="value">{total_kwh:,.0f}</div></div>
  </div>

  <section>
    <h2>Participacion de mercado por conectores</h2>
    <p class="desc">Solo conectores activos en el catalogo. Copec Voltex resaltado.</p>
    {bar_chart_svg(market_by_conn, "n_connectors", "op", "conectores", FOCUS_OPERATOR)}
    <div class="legend"><span><span class="swatch" style="background:var(--series-1)"></span>Otros operadores</span>
      <span><span class="swatch" style="background:var(--series-2)"></span>Copec Voltex</span></div>
    <details><summary>Ver tabla completa</summary>
      {table_view(market_by_conn, [("op", "Operador"), ("n_locations", "Locations"),
                                    ("n_connectors", "Conectores"), ("kw", "kW instalados"),
                                    ("pct_connectors", "% conectores")])}
    </details>
  </section>

  <section>
    <h2>Participacion de mercado por potencia instalada (kW)</h2>
    <p class="desc">Pondera por capacidad de carga, no solo cantidad de puntos.</p>
    {bar_chart_svg(market_by_kw, "kw", "op", "kW", FOCUS_OPERATOR)}
  </section>

  <section>
    <h2>kWh estimados por operador — ultimas {SESSIONS_WINDOW_H}h</h2>
    <p class="desc">Integracion trapezoidal de potencia instantanea durante cada sesion OCUPADO detectada.
      Sesiones con una sola lectura de potencia usan potencia constante como respaldo (ver metodo en la tabla).</p>
    {bar_chart_svg(sessions_sorted, "kwh", "op", "kWh", FOCUS_OPERATOR)}
    <details><summary>Ver tabla completa</summary>
      {table_view(sessions_sorted, [("op", "Operador"), ("n_sesiones", "Sesiones"), ("kwh", "kWh estimados")])}
    </details>
  </section>

  <section>
    <h2>Sesiones diarias — top {TOP_N_TREND} operadores ({TREND_DAYS} dias)</h2>
    {line_chart_svg(trend, top_ops, width=820)}
    <div class="legend">
      {"".join(f'<span><span class="swatch" style="background:var(--series-{i+1})"></span>{esc(op)}</span>' for i, op in enumerate(top_ops))}
    </div>
  </section>

  <section>
    <h2>Salud del pipeline <span class="badge {health_badge}">{esc(health_badge_txt)}</span></h2>
    <p class="desc">Ultimo sondeo: {esc(last_run_txt)} ({health["n_runs_24h"]} corridas en las ultimas 24h)</p>
    {error_html}
  </section>

  <footer>Generado automaticamente. institucion_privada NO se usa como filtro (ver notas del proyecto).</footer>
</div>
</body>
</html>'''


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Genera dist/index.html a partir del SQLite.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Ruta al archivo SQLite")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help="Ruta del HTML de salida")
    args = parser.parse_args()

    conn = get_db(args.db)
    html = render(conn)
    conn.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    print(f"Dashboard escrito en {args.out} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
