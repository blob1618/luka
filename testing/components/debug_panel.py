"""Debug panel — collapsible debug info per assistant message."""

import streamlit as st


def format_debug_for_export(debug_data: dict) -> dict:
    """
    Format debug data for JSON export.

    Ensures all fields are serializable. Missing fields become None.
    """
    return {
        "raw_json": debug_data.get("raw_json"),
        "latency_ms": debug_data.get("latency_ms"),
        "service_log": debug_data.get("service_log"),
        "redis_state": debug_data.get("redis_state"),
        "provider": debug_data.get("provider"),
        "prompt_used": debug_data.get("prompt_used"),
        "memory": debug_data.get("memory"),
        "memory_ttl_seconds": debug_data.get("memory_ttl_seconds"),
    }


def render_debug(debug_data: dict, flags: dict) -> None:
    """
    Render collapsible debug info below an assistant message.

    Only renders if at least one debug flag is active and there is data.
    """
    active_flags = any([
        flags.get("json"),
        flags.get("latency"),
        flags.get("redis"),
        flags.get("logs"),
    ])

    if not active_flags or not debug_data:
        return

    with st.expander("🔍 Debug", expanded=False):
        if flags.get("json") and debug_data.get("raw_json") is not None:
            st.subheader("JSON crudo LLM")
            st.json(debug_data["raw_json"])

        if flags.get("latency") and debug_data.get("latency_ms") is not None:
            st.metric("Latencia", f"{debug_data['latency_ms']:.0f}ms")

        if flags.get("logs") and debug_data.get("service_log"):
            st.subheader("Service invocado")
            st.code(debug_data["service_log"])

        if flags.get("redis") and debug_data.get("redis_state") is not None:
            st.subheader("Estado Redis")
            st.json(debug_data["redis_state"])

        memory = debug_data.get("memory")
        if flags.get("redis") and memory is not None:
            st.caption(
                f"Memoria conversacional: {len(memory)} mensajes · "
                f"TTL {debug_data.get('memory_ttl_seconds')}s"
            )
            st.json(memory)
