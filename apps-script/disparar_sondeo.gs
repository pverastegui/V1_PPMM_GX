/**
 * DESPERTADOR DEL SONDEO — no procesa ni guarda ningun dato, solo le avisa a
 * GitHub "corre el sondeo ahora". Toda la logica real (bajar la API, agrupar
 * operadores, calcular market share, etc.) vive en scripts/sondear.py, en el
 * repo de GitHub — a proposito, para no repetir el bug viejo de tener logica
 * de negocio duplicada entre Apps Script y otro lado.
 *
 * Por que existe esto: el `schedule` (cron) de GitHub Actions no es confiable
 * para disparos frecuentes — se salta o atrasa bajo carga. Los activadores
 * por tiempo de Apps Script si lo son. Asi que Apps Script hace de "alarma"
 * cada 5 minutos, y GitHub hace todo el trabajo pesado.
 *
 * --- COMO CONFIGURARLO (una sola vez) ---
 * 1. Crea un token en GitHub: Settings (de tu cuenta, no del repo) > Developer
 *    settings > Fine-grained tokens > Generate new token.
 *      - Repository access: "Only select repositories" > cargadores-monitor
 *      - Permissions: "Actions" = Read and write. Nada mas.
 *    Copia el token (empieza con "github_pat_...").
 * 2. En este proyecto de Apps Script: icono de engranaje "Project Settings" >
 *    "Script Properties" > "Add script property":
 *      Property: GITHUB_TOKEN
 *      Value:    (pega el token)
 * 3. Icono del reloj "Triggers" (a la izquierda) > "Add Trigger":
 *      Function: dispararSondeo
 *      Event source: Time-driven
 *      Type: Minutes timer
 *      Every: 5 minutes
 * 4. Corre dispararSondeo() una vez a mano desde el editor para autorizar los
 *    permisos (te va a pedir confirmar que puede hacer llamados a internet).
 */

const REPO = "pverastegui/cargadores-monitor";
const WORKFLOW = "sondear.yml";
const RAMA = "main";

function dispararSondeo() {
  const token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
  if (!token) {
    console.error("Falta configurar GITHUB_TOKEN en Project Settings > Script Properties.");
    return;
  }

  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const respuesta = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: "Bearer " + token,
      Accept: "application/vnd.github+json",
    },
    payload: JSON.stringify({ ref: RAMA }),
    muteHttpExceptions: true,
  });

  const codigo = respuesta.getResponseCode();
  // GitHub responde 204 (sin contenido) cuando el disparo salio bien.
  if (codigo !== 204) {
    console.error(`Fallo el disparo (HTTP ${codigo}): ${respuesta.getContentText()}`);
  }
}


/**
 * DESPERTADOR DEL RESUMEN — igual que dispararSondeo(), pero para resumir.yml, y una vez por HORA.
 *
 * Por que hace falta: resumir.yml tiene un `schedule` de GitHub (cada 30 min), pero GitHub no lo
 * cumple -- lo dispara unas 5 veces por dia en vez de 48 (cada 3 a 8 horas), por la misma razon por
 * la que existe el despertador del sondeo. Los resumenes quedaban desactualizados horas.
 *
 * Que NO hace: no toca dispararSondeo() ni el sondeo. Usa el mismo token (GITHUB_TOKEN) y el mismo
 * REPO/RAMA de arriba (el repo donde corre sondear.yml, que es donde vive resumir.yml).
 *
 * --- COMO ACTIVARLO (una sola vez) ---
 * 1. Pega esta funcion (y las dos de abajo) en el mismo proyecto de Apps Script del sondeo.
 * 2. Corre instalarActivadorResumen() una vez a mano: crea el activador por hora (cerca del minuto
 *    40) y no duplica si ya existe. O hazlo a mano: Triggers > Add Trigger > dispararResumen >
 *    Time-driven > Hour timer > Every hour.
 * 3. Para apagarlo: corre quitarActivadorResumen(), o borra el activador en Triggers. El sondeo no
 *    se ve afectado en ningun caso.
 */
const WORKFLOW_RESUMEN = "resumir.yml";

function dispararResumen() {
  const token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
  if (!token) {
    console.error("Falta configurar GITHUB_TOKEN en Project Settings > Script Properties.");
    return;
  }

  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_RESUMEN}/dispatches`;
  const respuesta = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: "Bearer " + token,
      Accept: "application/vnd.github+json",
    },
    payload: JSON.stringify({ ref: RAMA }),
    muteHttpExceptions: true,
  });

  const codigo = respuesta.getResponseCode();
  // GitHub responde 204 (sin contenido) cuando el disparo salio bien.
  if (codigo !== 204) {
    console.error(`Fallo el disparo del resumen (HTTP ${codigo}): ${respuesta.getContentText()}`);
  }
}

// Crea el activador por hora de dispararResumen (una vez). No duplica si ya existe.
function instalarActivadorResumen() {
  const ya = ScriptApp.getProjectTriggers().some(t => t.getHandlerFunction() === "dispararResumen");
  if (ya) {
    console.log("El activador del resumen ya existe: no se crea otro.");
    return;
  }
  ScriptApp.newTrigger("dispararResumen").timeBased().everyHours(1).nearMinute(40).create();
  console.log("Activador del resumen creado: cada hora, cerca del minuto 40.");
}

// Borra el activador del resumen (el del sondeo no se toca).
function quitarActivadorResumen() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === "dispararResumen")
    .forEach(t => ScriptApp.deleteTrigger(t));
  console.log("Activador del resumen eliminado.");
}
