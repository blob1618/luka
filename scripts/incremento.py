"""Inicialización y evidencia de incrementos de Luka."""

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import monotonic
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
INCREMENTS = ROOT / "docs" / "increments"
IDENTIFIER = re.compile(r"sprint-([0-9]{2}|pendiente)-(hu-[a-z0-9]+-[0-9]+|tecnico-[a-z0-9]+(?:-[a-z0-9]+)*)")
CHECKS = {
    "unit": ("pytest", "-q", "-m", "unit"),
    "webhook": ("pytest", "-q", "tests/test_webhook.py", "tests/test_webhook_idempotency.py"),
    "suite": ("pytest", "-v"),
    "lint": ("ruff", "check", "."),
}
SYSTEM_DIRS = ("app/", "prompts/", "supabase/migrations/", "testing/", "utils/")
SYSTEM_FILES = {"prompt.md", "requirements.txt", ".env.example", "Dockerfile", "render.yaml"}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8")


def identifier(value):
    if not IDENTIFIER.fullmatch(value):
        raise argparse.ArgumentTypeError("Usá sprint-NN-hu-xxx-nn o sprint-NN-tecnico-tema.")
    return value


def initialize(args):
    sheet = INCREMENTS / f"{args.ficha}.md"
    if sheet.exists():
        print(sheet.relative_to(ROOT).as_posix())
        return 0
    sprint, subject = IDENTIFIER.fullmatch(args.ficha).groups()
    story = subject.upper() if subject.startswith("hu-") else "Trabajo técnico"
    template = (INCREMENTS / "plantilla-incremento.md").read_text(encoding="utf-8")
    section = "## 1. Alcance y compromiso"
    start = datetime.now(UTC).date().isoformat()
    body = (
        template.split(section, 1)[1]
        .replace("[Indicador HU, título, descripción breve y beneficio para el usuario. Para trabajo técnico, propósito y HU a la que apoya si existe]",
                 f"{story} · {args.titulo}. [Descripción, valor o HU a la que apoya]", 1)
        .replace("[Número, período, Sprint Goal y aporte concreto de la HU]", f"{sprint.capitalize()} · [período, Sprint Goal y aporte]", 1)
        .replace("enlaces a Jira, PR del backend", f"Jira {args.jira}, PR del backend", 1)
        .replace("| Inicio efectivo del trabajo | [Fecha]", f"| Inicio efectivo del trabajo | {start}", 1)
    )
    heading = f"# Ficha de incremento — {story} · {args.titulo}"
    evidence = f"[Evidencias versionadas](evidencias/{args.ficha}/)"
    try:
        with sheet.open("x", encoding="utf-8", newline="\n") as output:
            output.write(f"{heading}\n\n{evidence}\n\n{section}{body}")
    except FileExistsError:
        pass
    print(sheet.relative_to(ROOT).as_posix())
    return 0


def record_check(args):
    if not (INCREMENTS / f"{args.ficha}.md").is_file():
        raise ValueError("Primero inicializá la ficha con el comando iniciar.")
    folder = INCREMENTS / "evidencias" / args.ficha
    folder.mkdir(parents=True, exist_ok=True)
    start = datetime.now(UTC)
    record = {
        "ficha": args.ficha,
        "fecha_utc": start.isoformat(timespec="seconds"),
        "commit_base": git("rev-parse", "HEAD").strip(),
        "cambios_locales": bool(git("status", "--porcelain", "--", ".", ":!docs/increments")),
        "comando": ["python", "-m", *CHECKS[args.control]],
        "python": sys.version.split()[0],
    }
    started = monotonic()
    result = subprocess.run([sys.executable, "-m", *CHECKS[args.control]], cwd=ROOT, check=False)
    record["codigo_salida"] = result.returncode
    record["duracion_segundos"] = round(monotonic() - started, 3)
    stamp = start.strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{stamp}-{args.control}-{uuid4().hex[:8]}.json"
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(f"Evidencia: {path.relative_to(ROOT).as_posix()}")
    return result.returncode


def verify(base, head):
    entries = git("diff", "--name-status", "--no-renames", "-z", f"{base}...{head}").split("\0")
    changes = dict(zip(entries[1::2], entries[::2]))
    sheets = set()
    evidence = set()
    for name, status in changes.items():
        if status == "D":
            continue
        path = PurePosixPath(name)
        if path.parts[:2] != ("docs", "increments"):
            continue
        if len(path.parts) == 3 and path.suffix == ".md" and IDENTIFIER.fullmatch(path.stem):
            sheet = ROOT / name
            if sheet.is_symlink() or not sheet.is_file() or not sheet.stat().st_size:
                raise ValueError(f"La ficha debe ser un archivo Markdown con contenido: {name}")
            sheets.add(path.stem)
        elif path.parts[2:3] == ("evidencias",):
            if len(path.parts) != 5 or not IDENTIFIER.fullmatch(path.parts[3]) or path.suffix not in {".json", ".md"}:
                raise ValueError(f"Ruta de evidencia inválida: {name}")
            artifact = ROOT / name
            sheet = INCREMENTS / f"{path.parts[3]}.md"
            if (
                not sheet.is_file()
                or sheet.is_symlink()
                or not artifact.is_file()
                or artifact.resolve() != artifact.absolute()
                or not artifact.stat().st_size
            ):
                raise ValueError(f"La evidencia debe tener contenido y una ficha asociada: {name}")
            if status == "A":
                evidence.add(path.parts[3])
    if any(name.startswith(SYSTEM_DIRS) or name in SYSTEM_FILES for name in changes):
        if not sheets or sheets - evidence:
            raise ValueError("Actualizá la ficha del incremento y agregá evidencia en su carpeta en este PR.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="accion", required=True)
    init = commands.add_parser("iniciar", help="Crear una ficha desde la plantilla o reutilizarla.")
    init.add_argument("ficha", type=identifier)
    init.add_argument("--titulo", required=True)
    init.add_argument("--jira", default="Pendiente")
    check = commands.add_parser("prueba", help="Ejecutar un control y guardar su resultado.")
    check.add_argument("ficha", type=identifier)
    check.add_argument("control", choices=CHECKS)
    guard = commands.add_parser("verificar", help="Comprobar la trazabilidad de los cambios de un PR.")
    guard.add_argument("base")
    guard.add_argument("head")
    args = parser.parse_args()
    try:
        if args.accion == "iniciar":
            return initialize(args)
        if args.accion == "prueba":
            return record_check(args)
        return verify(args.base, args.head)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    sys.exit(main())
