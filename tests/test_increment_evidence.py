from argparse import Namespace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import incremento


pytestmark = pytest.mark.unit
FICHA = "sprint-11-hu-reg-03"
TEMPLATE = Path(incremento.INCREMENTS, "plantilla-incremento.md")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    increments = tmp_path / "docs" / "increments"
    increments.mkdir(parents=True)
    (increments / TEMPLATE.name).write_bytes(TEMPLATE.read_bytes())
    monkeypatch.setattr(incremento, "ROOT", tmp_path)
    monkeypatch.setattr(incremento, "INCREMENTS", increments)
    return increments


def test_initialization_preserves_existing_work(workspace):
    args = Namespace(ficha=FICHA, titulo="Registrar fotos", jira="STK-123")
    assert incremento.initialize(args) == 0
    sheet = workspace / f"{FICHA}.md"
    original = sheet.read_text(encoding="utf-8")
    assert "HU-REG-03 · Registrar fotos" in original
    assert "STK-123" in original
    assert f"(evidencias/{FICHA}/)" in original

    sheet.write_text(original + "\nResultado observado.\n", encoding="utf-8")
    assert incremento.initialize(args) == 0
    assert sheet.read_text(encoding="utf-8").endswith("Resultado observado.\n")


def test_failed_check_keeps_evidence_and_propagates_exit_code(workspace, monkeypatch):
    (workspace / f"{FICHA}.md").write_text("# Ficha", encoding="utf-8")
    monkeypatch.setattr(incremento, "git", lambda *args: "abc123\n" if args[0] == "rev-parse" else " M app/main.py\n")
    monkeypatch.setattr(incremento.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))

    assert incremento.record_check(Namespace(ficha=FICHA, control="unit")) == 1
    path, = (workspace / "evidencias" / FICHA).glob("*.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["codigo_salida"] == 1
    assert record["commit_base"] == "abc123"
    assert record["cambios_locales"]


@pytest.mark.parametrize(
    ("code_status", "evidence_status", "passes"),
    [("", "", True), ("M", "", False), ("D", "M", False), ("M", "A", True)],
)
def test_runtime_pr_requires_fresh_evidence(workspace, monkeypatch, code_status, evidence_status, passes):
    sheet = workspace / f"{FICHA}.md"
    sheet.write_text("# Ficha", encoding="utf-8")
    evidence = workspace / "evidencias" / FICHA / "demo.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("# Resultado", encoding="utf-8")
    changes = ["M", sheet.relative_to(incremento.ROOT).as_posix()]
    if code_status:
        changes.extend((code_status, "app/main.py"))
    if evidence_status:
        changes.extend((evidence_status, evidence.relative_to(incremento.ROOT).as_posix()))
    monkeypatch.setattr(incremento, "git", lambda *args: "\0".join([*changes, ""]))

    if passes:
        assert incremento.verify("base", "head") == 0
    else:
        with pytest.raises(ValueError, match="Actualizá la ficha"):
            incremento.verify("base", "head")
