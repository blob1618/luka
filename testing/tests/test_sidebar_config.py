"""Tests for sidebar configuration logic (non-UI parts)."""

import os
from unittest.mock import patch


import testing.config.settings as settings_module
from testing.components.sidebar import get_available_models, get_available_prompts, get_available_providers
from testing.config.settings import new_session, next_session_label, phone_in_use, set_model_env


class TestGetAvailableProviders:
    def test_returns_list_from_factory(self):
        providers = get_available_providers()
        assert isinstance(providers, list)
        assert "gemini" in providers
        assert "mistral" in providers

    def test_reflects_factory_changes(self):
        with patch(
            "testing.components.sidebar._PROVIDERS",
            {"gemini": object, "mistral": object, "openai": object},
        ):
            providers = get_available_providers()
        assert "openai" in providers


class TestGetAvailablePrompts:
    def test_includes_default_prompt(self, tmp_path):
        prompts = get_available_prompts(str(tmp_path / "testing"))
        assert "prompt.md" in prompts

    def test_detects_testing_prompts_by_bare_name(self, tmp_path):
        prompts_dir = tmp_path / "testing" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "prompt_v2.md").write_text("# V2 prompt")
        (prompts_dir / "prompt_conciso.md").write_text("# Conciso")
        (prompts_dir / "not_a_prompt.txt").write_text("ignored")

        prompts = get_available_prompts(str(tmp_path / "testing"))
        assert "prompt.md" in prompts
        assert "prompt_v2.md" in prompts
        assert "prompt_conciso.md" in prompts
        assert "not_a_prompt.txt" not in prompts

    def test_detects_repo_prompts_with_relative_path(self, tmp_path):
        repo_prompts_dir = tmp_path / "prompts"
        repo_prompts_dir.mkdir()
        (repo_prompts_dir / "core_prompt.md").write_text("# Core")
        (repo_prompts_dir / "prompt_v3.md").write_text("# V3")
        (repo_prompts_dir / "not_a_prompt.txt").write_text("ignored")

        prompts = get_available_prompts(str(tmp_path / "testing"))
        assert "prompts/core_prompt.md" in prompts
        assert "prompts/prompt_v3.md" in prompts
        assert "prompts/not_a_prompt.txt" not in prompts

    def test_no_duplicates_and_sorted_repo_prompts(self, tmp_path):
        testing_prompts_dir = tmp_path / "testing" / "prompts"
        testing_prompts_dir.mkdir(parents=True)
        (testing_prompts_dir / "prompt.md").write_text("# duplicate name")
        repo_prompts_dir = tmp_path / "prompts"
        repo_prompts_dir.mkdir()
        (repo_prompts_dir / "b.md").write_text("# B")
        (repo_prompts_dir / "a.md").write_text("# A")

        prompts = get_available_prompts(str(tmp_path / "testing"))
        assert prompts.count("prompt.md") == 1
        assert len(prompts) == len(set(prompts))
        assert [p for p in prompts if p.startswith("prompts/")] == [
            "prompts/a.md",
            "prompts/b.md",
        ]

    def test_handles_missing_dirs(self, tmp_path):
        prompts = get_available_prompts(str(tmp_path / "nonexistent"))
        assert prompts == ["prompt.md"]


class TestTestingConfig:
    def test_prompt_path_default_es_core(self):
        assert settings_module.TestingConfig().prompt_path == "prompts/core_prompt.md"

    def test_sin_estado_global_de_usuario(self):
        config = settings_module.TestingConfig()
        assert not hasattr(config, "phone")
        assert not hasattr(config, "user_name")
        assert not hasattr(config, "user_registered")


class TestSessionHelpers:
    def test_next_session_label_usa_el_mayor_mas_uno(self):
        assert next_session_label([]) == "Sesión 1"
        sessions = [
            new_session("Sesión 1", "5491112345678", "A", True),
            new_session("Sesión 4", "5491187654321", "B", False),
        ]
        assert next_session_label(sessions) == "Sesión 5"

    def test_next_session_label_no_se_repite_al_borrar_una_del_medio(self):
        sessions = [
            new_session("Sesión 1", "5491111111111", "A", True),
            new_session("Sesión 3", "5491133333333", "C", True),
        ]
        assert next_session_label(sessions) == "Sesión 4"

    def test_phone_in_use_detecta_repetidos(self):
        sessions = [new_session("Sesión 1", "5491112345678", "A", True)]

        assert phone_in_use(sessions, "5491112345678") is True
        assert phone_in_use(sessions, "5491199999999") is False
        assert phone_in_use([], "5491112345678") is False

    def test_new_session_genera_id_unico_y_campos(self):
        first = new_session("Sesión 1", "5491112345678", "A", True)
        second = new_session("Sesión 2", "5491187654321", "B", False)

        assert first.id != second.id
        assert first.messages == []
        assert second.messages == []
        assert (second.label, second.phone, second.user_name, second.user_registered) == (
            "Sesión 2",
            "5491187654321",
            "B",
            False,
        )


class TestActiveSession:
    def test_devuelve_la_sesion_activa(self):
        first = new_session("Sesión 1", "5491112345678", "A", True)
        second = new_session("Sesión 2", "5491187654321", "B", False)
        config = settings_module.TestingConfig(sessions=[first, second], active_session_id=second.id)

        assert config.active_session() is second

    def test_devuelve_none_sin_sesion_activa(self):
        first = new_session("Sesión 1", "5491112345678", "A", True)

        assert settings_module.TestingConfig(sessions=[first], active_session_id="otro").active_session() is None
        assert settings_module.TestingConfig().active_session() is None


class TestGetAvailableModels:
    def test_gemini_flash_primero(self):
        models = get_available_models("gemini")
        assert models[0] == "gemini-3.6-flash"
        assert "gemini-3.7-flash" in models
        assert "gemini-3.1-flash-lite" in models
        assert "gemini-3.1-pro-preview" in models

    def test_mistral_small_primero(self):
        models = get_available_models("mistral")
        assert models[0] == "mistral-small-latest"
        assert "ministral-3b-latest" in models

    def test_provider_desconocido_devuelve_lista_gemini(self):
        assert get_available_models("desconocido") == get_available_models("gemini")


class TestSetModelEnv:
    def test_setea_gemini_model(self, monkeypatch):
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        set_model_env("gemini", "gemini-3.5-flash")
        assert os.environ["GEMINI_MODEL"] == "gemini-3.5-flash"

    def test_setea_mistral_model(self, monkeypatch):
        monkeypatch.delenv("MISTRAL_MODEL", raising=False)
        set_model_env("mistral", "mistral-small-latest")
        assert os.environ["MISTRAL_MODEL"] == "mistral-small-latest"

    def test_modelo_vacio_no_toca_env(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
        set_model_env("gemini", "")
        assert os.environ["GEMINI_MODEL"] == "gemini-3.1-flash-lite"
