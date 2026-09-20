"""Chat component — message rendering, input, and export."""

import asyncio
import json
import re
from html import escape as html_escape
from pathlib import Path

import streamlit as st
from streamlit.components.v1 import html as components_html

from testing.components.debug_panel import render_debug
from testing.config.settings import TestingConfig
from testing.services.webhook_mode import WebhookModeService


def _public_asset(filename: str) -> Path:
    """Resolve a file inside the testing/public/ directory."""
    return Path(__file__).resolve().parent.parent / "public" / filename


def bot_avatar() -> bytes:
    """Return the Luka logo as avatar bytes for the assistant."""
    logo = _public_asset("logo-luka.png")
    if logo.exists():
        return logo.read_bytes()
    return None


def export_as_json(messages: list[dict]) -> str:
    """Export chat history as JSON string."""
    return json.dumps(messages, ensure_ascii=False, indent=2)


def export_as_text(messages: list[dict], include_debug: bool = False) -> str:
    """Export chat history as human-readable text, optionally with LLM debug data."""
    if not messages:
        return ""
    lines = []
    for msg in messages:
        role_label = "Usuario" if msg["role"] == "user" else "Luka"
        lines.append(f"{role_label}: {msg['content']}")
        if include_debug and msg.get("debug"):
            debug_json = json.dumps(msg["debug"], ensure_ascii=False, indent=2)
            lines.append("\n".join(f"  {line}" for line in debug_json.splitlines()))
    return "\n".join(lines)


def copy_button_html(text: str, label: str) -> str:
    """Build a self-contained HTML/JS button that copies `text` to the clipboard."""
    payload = (
        json.dumps(text)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    safe_label = html_escape(label)
    return f"""
    <button data-label="{html_escape(label, quote=True)}" onclick="copyLukaChat(this)"
            style="width:100%;padding:0.4rem 0.75rem;border-radius:0.5rem;border:1px solid rgba(250,250,250,0.2);background:transparent;color:#fafafa;font-size:0.875rem;cursor:pointer;">
      {safe_label}
    </button>
    <script>
    function copyLukaChat(btn) {{
      const text = {payload};
      const done = () => {{
        btn.textContent = "✅ Copiado";
        setTimeout(() => {{ btn.textContent = btn.dataset.label; }}, 1500);
      }};
      if (navigator.clipboard && window.isSecureContext) {{
        navigator.clipboard.writeText(text).then(done).catch(() => legacyCopy(text, done));
      }} else {{
        legacyCopy(text, done);
      }}
    }}
    function legacyCopy(text, done) {{
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      done();
    }}
    </script>
    """


def render_copy_button(text: str, label: str) -> None:
    """Render a clipboard copy button for the given text."""
    components_html(copy_button_html(text, label), height=50)


_WHATSAPP_BOLD = re.compile(r"\*([^*\n]+)\*")


def whatsapp_to_markdown(text: str) -> str:
    """Convierte formato WhatsApp (`\\n`, `*negrita*`) a Markdown de Streamlit."""
    with_hard_breaks = text.replace("\n", "  \n")
    return _WHATSAPP_BOLD.sub(r"**\1**", with_hard_breaks)


def render_assistant_text(text: str) -> None:
    """Renderiza el texto del asistente respetando saltos de línea y negrita."""
    st.markdown(whatsapp_to_markdown(text))


def _get_prompt_path(config: TestingConfig) -> str:
    """Resolve prompt path from config."""
    if config.prompt_path == "prompt.md":
        return "prompt.md"
    return f"testing/prompts/{config.prompt_path}"


async def _process_message(text: str, config: TestingConfig) -> tuple[str, dict]:
    """
    Procesa el mensaje a través del flujo completo del dispatcher (webhook).

    Returns (reply_text, debug_data).
    """
    service = WebhookModeService()
    result = await service.send_message(
        text=text,
        phone=config.phone,
        provider=config.provider,
        prompt_path=_get_prompt_path(config),
        model=config.model,
    )
    debug_data = {
        "raw_json": result.raw_llm_response,
        "latency_ms": result.latency_ms,
        "service_log": result.service_invoked or "unknown",
        "redis_state": result.redis_state,
        "provider": result.provider,
        "model": result.model,
        "prompt_used": result.prompt_path,
    }
    return result.reply_text, debug_data


def render_chat(config: TestingConfig) -> None:
    """Render the chat interface and handle user input."""

    # Display existing messages
    for msg in st.session_state.messages:
        if msg["role"] == "assistant":
            with st.chat_message("assistant", avatar=bot_avatar()):
                render_assistant_text(msg["content"])
                if msg.get("debug"):
                    flags = {
                        "json": config.debug_json,
                        "latency": config.debug_latency,
                        "redis": config.debug_redis,
                        "logs": config.debug_logs,
                    }
                    render_debug(msg["debug"], flags)
        else:
            with st.chat_message("user"):
                st.write(msg["content"])

    # Chat input
    if prompt := st.chat_input("Escribí un mensaje..."):
        # Add user message
        st.session_state.messages.append({
            "role": "user",
            "content": prompt,
            "debug": {},
        })
        with st.chat_message("user"):
            st.write(prompt)

        # Process and add assistant response
        with st.chat_message("assistant", avatar=bot_avatar()):
            with st.spinner("Procesando..."):
                reply_text, debug_data = asyncio.run(
                    _process_message(prompt, config)
                )

            raw = debug_data.get("raw_json") or {}
            if raw.get("error"):
                st.error(reply_text or "Error del LLM")
            else:
                render_assistant_text(reply_text or "Sin respuesta")

            flags = {
                "json": config.debug_json,
                "latency": config.debug_latency,
                "redis": config.debug_redis,
                "logs": config.debug_logs,
            }
            render_debug(debug_data, flags)

        st.session_state.messages.append({
            "role": "assistant",
            "content": reply_text or "Sin respuesta",
            "debug": debug_data,
        })

    # Export buttons in sidebar
    with st.sidebar:
        if st.session_state.messages:
            st.subheader("Exportar")
            json_data = export_as_json(st.session_state.messages)
            st.download_button(
                "💾 JSON",
                data=json_data,
                file_name="luka_test_chat.json",
                mime="application/json",
                key="export_json",
            )
            text_data = export_as_text(st.session_state.messages)
            st.download_button(
                "📄 Texto",
                data=text_data,
                file_name="luka_test_chat.txt",
                mime="text/plain",
                key="export_text",
            )
            render_copy_button(text_data, "📋 Copiar conversación")
            render_copy_button(
                export_as_text(st.session_state.messages, include_debug=True),
                "🐞 Copiar con debug",
            )
