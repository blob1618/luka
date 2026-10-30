import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

from app.services.telemetry import track_phase
from app.services.llm_providers import LLMProvider, create_provider

_FINANCIAL_REPLY_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "financial_reply_prompt.md"
)
_FACT_TOKEN = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
_QUOTE_TYPE_WORD = re.compile(r"\b(?:oficiales|oficial|mep|blue)\b", re.IGNORECASE)
_QUOTE_TYPE_ALIASES = {
    "oficial": "oficial",
    "oficiales": "oficial",
    "mep": "mep",
    "blue": "blue",
}
_NET_WORTH_FORBIDDEN = re.compile(
    r"\b(?:compra|venta|DolarAPI|consultad[oa]s?|actualizad[oa]s?)\b", re.IGNORECASE
)


@lru_cache(maxsize=1)
def _financial_reply_prompt() -> str:
    return _FINANCIAL_REPLY_PROMPT_PATH.read_text(encoding="utf-8")


def _financial_reply_fallback(facts: Any) -> str:
    """Lista determinística con los facts; nunca lanza y siempre es `str`."""
    lines: list[str] = []
    try:
        for fact in facts:
            lines.append(f"• {fact['label']}: {fact['value']}")
    except Exception:
        pass
    return "\n".join(lines)


def _violates_financial_reply_contract(
    reply_text: Any,
    facts: list[Dict[str, str]],
    quote_types: list[str],
    kind: str,
) -> bool:
    """True si el texto generado no respeta el contrato de placeholders."""
    if not isinstance(reply_text, str) or not reply_text.strip():
        return True

    keys = [fact["key"] for fact in facts]
    tokens = _FACT_TOKEN.findall(reply_text)
    if any(token not in keys for token in tokens):
        return True
    if any(tokens.count(key) != 1 for key in keys):
        return True

    # Todo placeholder debe ser canónico; el resto del texto no puede tener llaves.
    prose = _FACT_TOKEN.sub(" ", reply_text)
    if "{" in prose or "}" in prose:
        return True

    if any(char.isdigit() for char in prose):
        return True

    for line in reply_text.splitlines():
        if _FACT_TOKEN.search(line) and not line.startswith("• "):
            return True

    requested = {
        _QUOTE_TYPE_ALIASES[item]
        for item in (quote_type.strip().lower() for quote_type in quote_types)
        if item in _QUOTE_TYPE_ALIASES
    }
    for match in _QUOTE_TYPE_WORD.finditer(prose):
        if _QUOTE_TYPE_ALIASES[match.group(0).lower()] not in requested:
            return True

    return kind == "net_worth" and bool(_NET_WORTH_FORBIDDEN.search(prose))


class LLMService:
    """
    Fachada pública para procesamiento LLM.

    Carga el system prompt desde prompt.md y delega la comunicación
    con el proveedor a un LLMProvider concreto seleccionado por la
    env var LLM_PROVIDER (default: 'gemini').
    """

    _provider: LLMProvider | None = None
    _system_prompt: str | None = None
    _prompt_path: str | None = None

    @classmethod
    def set_prompt_path(cls, path: str | None) -> None:
        """Sobreescribe la ruta al prompt (útil para tests)."""
        cls._prompt_path = path
        cls._system_prompt = None  # force reload

    @classmethod
    def _load_system_prompt(cls) -> str:
        """
        Carga el system prompt.
        Si se definió una ruta explícita (set_prompt_path o SYSTEM_PROMPT_PATH),
        se intenta cargar desde allí. En la ruta predeterminada, busca primero
        prompts/core_prompt.md y luego prompt.md. Si ninguna existe, usa
        un prompt de fallback interno. Se cachea en memoria tras la primera carga.
        """
        if cls._system_prompt is not None:
            return cls._system_prompt

        explicit_path = cls._prompt_path or os.getenv("SYSTEM_PROMPT_PATH")
        if explicit_path:
            candidate_paths = [Path(explicit_path)]
        else:
            root = Path(__file__).resolve().parent.parent.parent
            candidate_paths = [
                root / "prompts" / "core_prompt.md",
                root / "prompt.md",
            ]

        for path in candidate_paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cls._system_prompt = f.read()
                    return cls._system_prompt
            except FileNotFoundError:
                continue

        print(
            f"[LLMService] No prompt file found at {[str(p) for p in candidate_paths]}, "
            "using fallback prompt."
        )
        cls._system_prompt = (
            "Eres LUKA, un asistente financiero personal que opera por WhatsApp. "
            "Ayudas a los usuarios a registrar y gestionar sus gastos personales. "
            "Responde siempre en español, de forma amable y concisa. "
            "No des consejos financieros profesionales ni temas no relacionados."
        )

        return cls._system_prompt

    @classmethod
    def _get_provider(cls) -> LLMProvider:
        """Singleton: instancia el provider la primera vez que se necesita."""
        if cls._provider is None:
            cls._provider = create_provider()
        return cls._provider

    @classmethod
    def reset_provider(cls) -> None:
        """
        Singleton: Re-creación del provider en el próximo uso.
        Para usar distintos providers sin estado compartido (tests).
        """
        cls._provider = None

    @classmethod
    async def render_financial_reply(
        cls,
        kind: str,
        facts: list[Dict[str, str]],
        quote_types: list[str],
    ) -> str:
        """
        Redacta una respuesta financiera donde el LLM solo ubica placeholders.

        Los valores salen siempre de `facts`, nunca del texto generado. Si la
        salida viola el contrato se reintenta una vez; ante cualquier fallo se
        devuelve una lista determinística con los mismos facts.
        """
        fallback = _financial_reply_fallback(facts)
        try:
            system_prompt = _financial_reply_prompt()
            user_message = json.dumps(
                {"kind": kind, "quote_types": quote_types, "facts": facts},
                ensure_ascii=False,
            )
            for _ in range(2):
                with track_phase('llm'):
                    parsed = await cls._get_provider().generate_json(
                        system_prompt=system_prompt,
                        user_message=user_message,
                        temperature=0.1,
                        history=None,
                    )
                reply_text = parsed.get("reply_text") if isinstance(parsed, dict) else None
                if not _violates_financial_reply_contract(reply_text, facts, quote_types, kind):
                    values = {fact["key"]: fact["value"] for fact in facts}
                    return _FACT_TOKEN.sub(lambda match: values[match.group(1)], reply_text)
            return fallback
        except Exception as exc:
            print(f"[LLMService] render_financial_reply failed: {type(exc).__name__}")
            return fallback
