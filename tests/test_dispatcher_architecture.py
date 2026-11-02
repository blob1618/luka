"""Architecture guard: the dispatcher stays a thin, intention-free ingress."""
import ast
from pathlib import Path

import pytest

from app.services import dispatcher

pytestmark = pytest.mark.unit


def test_dispatcher_has_no_regex_or_domain_routing():
    source = Path(dispatcher.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "re" not in imported
    assert "unicodedata" not in imported
    assert "_is_calendar_connect" not in source
    assert "_handle_calendar_connect" not in source

    functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert functions == {
        "_enter",
        "process_incoming_message",
        "process_incoming_interactive_reply",
    }
