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
