# Monitor de Cargadores Publicos — Copec Voltex

Pipeline en Python que sondea `https://cargadorespublicos.cl/api/data` (la
plataforma de interoperabilidad de la SEC), guarda el historial en SQLite
dentro del propio repo de git, y publica un dashboard estatico con
participacion de mercado, sesiones de carga y kWh estimados por operador.

## Por que existe esto

Reemplaza un Apps Script que sondeaba cada 5 minutos y escribia a Google
Sheets. Dos problemas de ese script quedaron resueltos ademas de cambiar de
plataforma:

1. **Filtraba por `institucion_privada`**, que indica si el *sitio* pertenece
   a una institucion privada, no si el operador de carga es publico o
   privado. Ese filtro descartaba ~22% de las locations de la API, incluido
   11% de las propias de Copec Voltex. Aca **no se filtra por ese campo**.
2. **No tenia manejo de errores de red.** Un timeout o una respuesta
   corrupta tumbaba la ejecucion sin dejar rastro util. Aca toda llamada de
   red va con reintentos + backoff, y **cada corrida queda registrada en la
   tabla `poll_runs`** (exitosa o no) — hay un panel de salud del pipeline
   en el dashboard mismo, no hay que ir a buscar logs.

## Arquitectura

```
GitHub Actions (cron cada 15 min)
  -> scripts/poller.py           sondea la API, actualiza data/cargadores.db, comitea
  -> (push a main dispara)
GitHub Actions (on push)
  -> scripts/build_dashboard.py  lee el SQLite, genera dist/index.html
  -> cloudflare/wrangler-action  publica dist/ en Cloudflare Pages
```

El repo de git ES la base de datos versionada (patron "git scraping"): cada
sondeo que trae cambios queda como un commit de `data/cargadores.db`.

## Estructura

```
scripts/
  poller.py           logica de ingesta (testeable sin red, ver ingest_snapshot)
  build_dashboard.py  genera el HTML del dashboard desde el SQLite
tests/
  test_poller.py       pruebas de humo (sesiones, kWh, reintentos, retiro de conectores)
  seed_demo_data.py    genera tests/demo_cargadores.db con datos SINTETICOS
                        para poder mirar el dashboard sin esperar datos reales
data/
  cargadores.db        la base real - la crea y actualiza el pipeline, no a mano
.github/workflows/
  ingest.yml           cron cada 15 min
  deploy.yml           build + publish en Cloudflare Pages
```

## Modelo de datos (SQLite)

- `connectors`: catalogo, una fila por conector visto alguna vez (`active=0`
  cuando desaparece de la API por mas de 24h seguidas).
- `status_events`: log append-only de transiciones de estado.
- `power_readings`: lecturas de potencia instantanea, **solo mientras un
  conector esta OCUPADO** (para no guardar millones de filas de conectores
  inactivos que no cambian).
- `sessions`: una fila por sesion de carga (abierta o cerrada). El `kWh`
  estimado se calcula por **integracion trapezoidal** de `power_readings`
  dentro de la sesion; si solo hay una lectura de potencia, se usa potencia
  constante como respaldo (columna `estimation_method` deja registrado cual
  de los dos se uso, para que sepan cuanto confiar en cada numero).
- `poll_runs`: una fila por ejecucion del poller, exitosa o no.

## Correr las pruebas

```bash
pip install -r requirements.txt
python tests/test_poller.py
```

## Ver el dashboard con datos de ejemplo (sin tocar la API real)

```bash
python tests/seed_demo_data.py
python scripts/build_dashboard.py --db tests/demo_cargadores.db --out /tmp/demo.html
```

Abran `/tmp/demo.html` en el navegador.

## Puesta en marcha (una sola vez)

1. **Crear el repo en GitHub como privado.** Los numeros de participacion de
   mercado son informacion competitiva - no conviene un repo publico aunque
   los datos crudos de la API sean publicos por ley.

2. **Primer sondeo**, para que `data/cargadores.db` exista antes de correr
   el deploy por primera vez:
   ```bash
   python scripts/poller.py
   git add data/cargadores.db && git commit -m "data: primer sondeo" && git push
   ```

3. **Cloudflare Pages + Access** (dashboard privado, con URL viva):
   - Crear una cuenta de Cloudflare (gratis) si no tienen una.
   - Crear el proyecto de Pages: `wrangler pages project create cargadores-voltex-monitor`
     (o desde el dashboard de Cloudflare, seccion Pages > Create).
   - En **My Profile > API Tokens**, crear un token con permiso
     "Cloudflare Pages: Edit". Copiar el Account ID (aparece en la barra
     lateral derecha de cualquier zona/dashboard).
   - En el repo de GitHub: **Settings > Secrets and variables > Actions**,
     agregar `CLOUDFLARE_API_TOKEN` y `CLOUDFLARE_ACCOUNT_ID`.
   - **Restringir el acceso** (importante - un sitio de Pages es publico por
     defecto): activar Cloudflare Zero Trust (tiene capa gratis hasta 50
     usuarios), ir a **Access > Applications > Add an application > Self-hosted**,
     apuntarlo al dominio `*.pages.dev` del proyecto, y crear una politica
     "Allow" por regla `Emails ending in` -> `@copec.cl`. Con eso, cualquiera
     de su dominio entra con un codigo de un solo uso al correo, y nadie mas
     puede ver el dashboard aunque tenga el link.

4. **Activar los workflows**: con el push del paso 2, `ingest.yml` ya deberia
   empezar a correr cada 15 minutos solo. `deploy.yml` se dispara automatico
   cuando `ingest.yml` comitea un cambio en `data/cargadores.db`.

5. (Opcional) **Notificacion de fallas**: GitHub ya puede avisarles por
   email cuando un workflow falla (cada persona lo activa en su propia
   cuenta: Settings > Notifications > Actions). Si prefieren Slack, se
   agrega un step extra en `ingest.yml` con un webhook - avisen si lo
   quieren y se los dejamos armado.

## Decisiones de diseño y limitaciones a tener presente

- **Cadencia: 15 minutos, no 1-2.** Sondear cada 1-2 minutos 24/7 son
  ~43.000 min/mes de computo, muy por sobre el free tier de Actions en repo
  privado (~2.000 min/mes). A 15 minutos son ~96 corridas/dia, comodo en el
  free tier. El costo es precision: dentro de una sesion de carga se capturan
  menos puntos de la curva de potencia, asi que el kWh estimado es una
  aproximacion razonable, no una medicion exacta (para eso haria falta el
  medidor del propio operador). Si mas adelante necesitan mas precision,
  la migracion natural es un proceso siempre encendido (una VM chica) en vez
  de apretar mas el cron de Actions.
- **El cron de Actions no es al segundo.** GitHub puede atrasar el disparo
  en momentos de carga alta (tipicamente en punto de la hora). Para esta
  metrica (tendencias de mercado, no facturacion) no es un problema.
- **Tamaño esperado de `data/cargadores.db`**: con ~2.265 conectores activos
  hoy en la API y sondeo cada 15 min, la proyeccion (basada en una
  simulacion con tasas de cambio de estado realistas) es de un orden de
  magnitud de **~100-250 MB/mes**, es decir **~1.5-3 GB/año** sin podar
  nada. Es perfectamente manejable para git, pero si en un año se sienten
  incomodos con el tamaño del repo, las salidas son: podar `power_readings`
  viejas ya cerradas en `sessions` (son la tabla que mas crece) y correr
  `VACUUM`, o mover a Git LFS. No hace falta resolverlo ahora.
- **`electric_power` en la API se toma como lectura real de potencia
  instantanea** durante `OCUPADO`. Si en algun momento notan que el kWh
  estimado no calza con las boletas/reportes de un operador conocido (por
  ejemplo comparando con datos propios de Copec Voltex), vale la pena
  validar ese supuesto contra un caso real antes de reportar el numero a
  gerencia.
