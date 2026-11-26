"""
Prueba del paso "Guardar los resumenes en la rama resumen" de resumir.yml, sin red: se ejecuta el MISMO
script que corre el workflow contra un repo "remoto" local, y se comprueba que:
  - la rama `resumen` queda huerfana (1 commit) con SOLO los 2 archivos de resumen,
  - `main` del remoto no se toca (ni se pierde un commit del sondeo hecho en paralelo),
  - si no hay cambios no se vuelve a publicar nada,
  - si cambian, la rama se reemplaza sin crecer el historial,
  - si el push falla, el paso FALLA (no se ignora en silencio como el `|| true` del sondeo).
Corre con: python tests/test_resumir_rama.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
WORKFLOW = RAIZ / ".github" / "workflows" / "resumir.yml"
NOMBRE_PASO = 'Guardar los resumenes en la rama "resumen"'


def script_del_paso() -> str:
    """Extrae el bloque `run: |` del paso, tal cual lo ejecuta GitHub (sin las indentaciones del YAML)."""
    lineas = WORKFLOW.read_text(encoding="utf-8").splitlines()
    ini = next(i for i, l in enumerate(lineas) if NOMBRE_PASO in l)
    run = next(i for i in range(ini, len(lineas)) if lineas[i].strip() == "run: |")
    sangria = len(lineas[run]) - len(lineas[run].lstrip()) + 2
    cuerpo = []
    for l in lineas[run + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) < sangria:
            break
        cuerpo.append(l[sangria:] if l.strip() else "")
    return "\n".join(cuerpo)


def git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} fallo: {r.stderr}")
    return r.stdout.strip()


def correr_paso(carpeta, tmp, espera="0"):
    env = dict(os.environ, RUNNER_TEMP=str(tmp), ESPERA_REINTENTO=espera)
    # GitHub corre los pasos con: bash --noprofile --norc -eo pipefail {0}
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script_del_paso()],
                          cwd=carpeta, env=env, capture_output=True, text=True)


def main():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        remoto = tmp / "remoto.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(remoto))
        url = f"file://{remoto}"

        # --- el "sondeo": deja datos en main ---
        sondeo = tmp / "sondeo"
        git(tmp, "clone", "-q", url, str(sondeo))
        git(sondeo, "config", "user.name", "sondeo"); git(sondeo, "config", "user.email", "s@x")
        (sondeo / "data" / "eventos").mkdir(parents=True)
        (sondeo / "snapshots").mkdir()
        (sondeo / "data" / "catalogo.csv").write_text("a,b\n1,2\n")
        (sondeo / "snapshots" / "x.json.gz").write_text("crudo")
        git(sondeo, "add", "-A"); git(sondeo, "commit", "-qm", "sondeo 1"); git(sondeo, "push", "-q", "origin", "main")

        # --- el runner del resumen: checkout superficial del repo privado + resumenes recien generados ---
        priv = tmp / "datos-privados"
        git(tmp, "clone", "-q", "--depth", "1", url, str(priv))
        (priv / "data").mkdir(exist_ok=True)
        (priv / "data" / "resumen_diario.csv").write_text("vista,fecha,empresa\nopc,2026-11-02,X\n")
        (priv / "data" / "resumen_parque.csv").write_text("vista,empresa\nopc,X\n")
        main_antes = git(remoto, "rev-parse", "main")

        # 1) primera publicacion
        r = correr_paso(priv, tmp)
        assert r.returncode == 0, r.stderr
        archivos = git(remoto, "ls-tree", "-r", "--name-only", "resumen").splitlines()
        assert archivos == ["data/resumen_diario.csv", "data/resumen_parque.csv"], archivos
        assert git(remoto, "rev-list", "--count", "resumen") == "1"
        assert git(remoto, "rev-list", "--parents", "-n", "1", "resumen").split() == [git(remoto, "rev-parse", "resumen")], \
            "la rama debe ser huerfana (commit sin padre)"
        assert git(remoto, "rev-parse", "main") == main_antes, "main no debe tocarse"
        assert git(priv, "diff", "--cached", "--name-only") == "", "el indice real del checkout no debe tocarse"
        print("OK: 1.a publicacion -> rama resumen huerfana, solo los 2 archivos, main intacto")

        # 2) sin cambios: no publica de nuevo
        h1 = git(remoto, "rev-parse", "resumen")
        r = correr_paso(priv, tmp)
        assert r.returncode == 0 and "Sin cambios" in r.stdout, r.stdout + r.stderr
        assert git(remoto, "rev-parse", "resumen") == h1
        print("OK: sin cambios no se vuelve a publicar")

        # 3) el sondeo empuja a main en paralelo; el resumen cambia y se vuelve a publicar
        (sondeo / "data" / "catalogo.csv").write_text("a,b\n1,3\n")
        git(sondeo, "commit", "-qam", "sondeo 2"); git(sondeo, "push", "-q", "origin", "main")
        sondeo_hash = git(remoto, "rev-parse", "main")
        (priv / "data" / "resumen_diario.csv").write_text("vista,fecha,empresa\nopc,2026-11-02,Y\n")
        r = correr_paso(priv, tmp)
        assert r.returncode == 0, r.stderr
        assert git(remoto, "rev-parse", "main") == sondeo_hash, "el resumen no debe alterar el push del sondeo"
        assert git(remoto, "rev-parse", "resumen") != h1
        assert git(remoto, "rev-list", "--count", "resumen") == "1", "el historial de resumen no debe crecer"
        assert "Y" in git(remoto, "show", "resumen:data/resumen_diario.csv")
        print("OK: con cambios se reemplaza la rama (sigue 1 commit) y el push del sondeo a main queda intacto")

        # 4) el sondeo puede seguir empujando a main sin chocar con la rama resumen
        (sondeo / "data" / "catalogo.csv").write_text("a,b\n1,4\n")
        git(sondeo, "commit", "-qam", "sondeo 3"); git(sondeo, "push", "-q", "origin", "main")
        print("OK: el sondeo empuja a main despues sin problema")

        # 5) si el push falla, el paso falla (no se ignora)
        git(priv, "remote", "set-url", "origin", "file:///no/existe.git")
        (priv / "data" / "resumen_parque.csv").write_text("vista,empresa\nopc,Z\n")
        r = correr_paso(priv, tmp)
        assert r.returncode != 0 and "No se pudo guardar" in r.stderr, (r.returncode, r.stderr)
        print("OK: si no se puede publicar, el paso falla de forma visible")
    print("\nTodas las pruebas pasaron.")


if __name__ == "__main__":
    sys.exit(main())
