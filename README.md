# Monitor de cargadores públicos de Chile

Guarda cada 5 minutos el estado de toda la red de carga pública del país, para
poder calcular después transacciones, volumen y participación de mercado por
operador.

Fuente: `https://cargadorespublicos.cl/api/data` — plataforma de
interoperabilidad de la SEC (Decreto Supremo N°12, Ministerio de Energía).

---

## Por qué existe

La API entrega una **foto del momento**: qué conector está ocupado y cuál está
libre, ahora. No tiene memoria. Si nadie la consulta y anota el resultado, esa
información se pierde para siempre — no hay forma de preguntarle después
"cuántas cargas hubo el martes pasado".

Este repo es exactamente eso: un robot que mira cada 5 minutos y anota.

## Cómo funciona, en una frase por paso

Cada 5 minutos, GitHub Actions ejecuta `scripts/sondear.py`, que:

1. **Baja la API.** Si falla (timeout, error de red, JSON corrupto), reintenta
   3 veces con espera creciente.
2. **Guarda el crudo comprimido**, tal como vino, en
   `snapshots/2026-09-01/1835.json.gz`. Esto pasa **antes** de procesar
   cualquier cosa: si el procesamiento tuviera un bug, el dato original ya está
   a salvo y se puede reprocesar.
3. **Compara con la foto anterior** (que vive en `data/catalogo.csv`) y, por
   cada conector que cambió de estado, agrega una fila a
   `data/eventos/2026-09.csv`.
4. **Anota la corrida** en `data/corridas.csv`, haya funcionado o no.

Una sesión de carga se deduce de los cambios de estado: cuando un conector pasa
de `DISPONIBLE` a `OCUPADO`, empezó una carga. Cuando vuelve a `DISPONIBLE`,
terminó. Por eso lo que interesa contar son las filas con
`estado_nuevo = OCUPADO`.

## Los archivos

| Archivo | Qué es | Se borra? |
|---|---|---|
| `snapshots/AAAA-MM-DD/HHMM.json.gz` | El crudo, sin tocar. Un archivo por sondeo. | Sí, a los 60 días (automático) |
| `data/catalogo.csv` | Una fila por conector conocido, con su estado actual. Se reescribe cada corrida. | No |
| `data/eventos/AAAA-MM.csv` | Los cambios de estado. Un archivo por mes. Solo crece. | Nunca |
| `data/corridas.csv` | Una fila por ejecución, con `ok` = 1 o 0 y el error si hubo. | Nunca |
| `mapeo_operadores.csv` | Tabla que agrupa razones sociales del mismo operador. **Se edita a mano.** | Nunca |

Todo en CSV a propósito: se abren directo en Google Sheets o Excel, sin
instalar nada.

## Las columnas de `eventos` calzan con la planilla

Las primeras 12 columnas están en el **mismo orden** que la hoja `Eventos` de la
planilla, así que las fórmulas de la guía siguen funcionando sin cambios:

| Letra | Columna | | Letra | Columna |
|---|---|---|---|---|
| A | `timestamp_deteccion` | | G | `power_type` |
| B | `connector_id` | | H | `max_electric_power` |
| C | `operator_name` | | I | `standard` |
| D | `estado_anterior` | | J | `location_name` |
| E | `estado_nuevo` | | K | `operador_agrupado` |
| F | `api_last_updated` | | L | `tramo_potencia` |

Y hay dos columnas nuevas al final, que no corren nada de lugar: `M` = `commune`,
`N` = `region`.

**Lo que cambia para bien:** las columnas **K** (operador agrupado) y **L**
(tramo de potencia) antes eran fórmulas `ARRAYFORMULA` con `BUSCARV` dentro de la
planilla — las dos más frágiles de mantener. Ahora vienen ya calculadas desde el
script. Se pueden borrar esas fórmulas de la planilla.

Los tramos de potencia son los mismos: `7`, `(7-22]`, `(22-50]`, `(50-150]`,
`150`. Y el tramo más alto sigue escribiéndose `150` a secas, nunca `>150`, por
la misma razón de siempre (Sheets lee el `>` como una condición y el conteo da
cero).

## Mantención: lo único que hay que hacer a mano

Cuando aparezca un operador nuevo reportado con varios nombres distintos, se
agrega una fila a `mapeo_operadores.csv`:

```csv
nombre_original,nombre_agrupado
COPEC S.A.,Copec Voltex
```

Si un operador no está en la tabla, se usa su nombre tal cual — no rompe nada,
solo aparece "suelto" en los análisis. Esa es la señal para agregarlo.

Nota: la API ya trae un campo oficial normalizado (`OPC.normalized_name`), que
es el que se usa primero. El mapeo solo hace falta para los casos donde ese
campo viene vacío y hay que caer al nombre del dueño, que es donde aparecen las
variantes.

## Espacio que ocupa

Cada snapshot crudo pesa entre 50 y 120 KB comprimido (el JSON sin comprimir son
~2,9 MB, pero es muy repetitivo y gzip lo reduce muchísimo). A 288 sondeos por
día:

- crudo: ~15-35 MB por día, que con la ventana de 60 días **se estabiliza en
  torno a 1-2 GB** y deja de crecer
- `eventos`: unas decenas de MB por mes, permanente
- `catalogo.csv`: ~560 KB fijos (se reescribe, no crece)

`scripts/limpiar_crudos.py` corre una vez al día y borra las carpetas de
snapshots con más de 60 días. Los CSVs nunca se tocan.

## Correr las pruebas

```bash
pip install -r requirements.txt
python tests/test_sondear.py
```

Son 8 pruebas y no necesitan internet: le pasan datos falsos con la forma real
de la API. Cubren el ciclo completo de una sesión, los tramos de potencia, el
orden de las columnas, el retiro de conectores y qué pasa si la API manda algo
malformado.

## Correr un sondeo a mano

```bash
python scripts/sondear.py
```

Si estás en la red de Copec puede fallar con `SSLError` (el proxy corporativo
intercepta el certificado). En los servidores de GitHub eso no pasa, porque
están fuera de la red de la empresa.

## Notas de diseño

**Por qué el repo es público.** GitHub cobra los minutos de Actions redondeando
hacia arriba al minuto por cada corrida. A 5 minutos son 288 corridas al día =
~8.640 minutos al mes, muy por sobre los 2.000 gratis de un repo privado
(costaría ~US$40/mes). En repo público el cómputo es gratis e ilimitado. La data
cruda de la API ya es pública por ley, así que no se está exponiendo nada
reservado — pero conviene tenerlo presente antes de subir análisis internos acá.

**Por qué no se filtra `institucion_privada`.** El script anterior descartaba las
locations con ese campo en `true`, asumiendo que distinguía operadores públicos
de privados. En realidad indica si el **sitio** pertenece a una institución
privada (un mall, una estación de servicio). Ese filtro descartaba 146 de 652
locations (22% del total), incluidas 28 de las 252 de Copec Voltex. Acá no se
filtra; la columna se guarda en el catálogo por si alguna vez se necesita.

**Por qué el cron puede atrasarse.** GitHub no garantiza el minuto exacto y puede
demorar el disparo cuando tiene mucha carga, sobre todo en punto de la hora. Para
medir tendencias de mercado no es problema; sí significa que la duración de una
sesión tiene un margen de error de algunos minutos.
