# testing/streamlit_app.py
"""Luka Testing Environment — Streamlit entry point."""

import os
import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Cargar el .env de la raíz (API keys de LLM, etc.) ANTES de importar submódulos,
# igual que app/main.py. En Docker las keys llegan vía env_file del compose.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Testing environment always uses its isolated SQLite database.
os.environ["DATABASE_URL"] = "sqlite:///./testing_luka.db"

from app.models.database import engine  # noqa: E402
from testing.services.schema import ensure_testing_schema  # noqa: E402

ensure_testing_schema(engine)

import streamlit as st  # noqa: E402

from testing.config.settings import TestingConfig  # noqa: E402
from testing.components.sidebar import render_sidebar  # noqa: E402
from testing.components.chat import render_chat  # noqa: E402
from testing.services.user_simulator import UserSimulator  # noqa: E402

st.set_page_config(
    page_title="Luka Testing",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0d1117 0%, #161b22 100%);
    }
    [data-testid="stSidebar"] img {
        border-radius: 8px;
    }
    .stApp {
        background-color: #0d1117;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Initialize session state
if "messages" not in st.session_state:
    st.session_state.messages = []
if "config" not in st.session_state:
    st.session_state.config = TestingConfig()
if "user_simulator_initialized" not in st.session_state:
    st.session_state.user_simulator_initialized = False

# Render sidebar and get config
config = render_sidebar()

# Handle DB reset
if st.session_state.get("reset_db_requested"):
    from app.models.database import SessionLocal
    sim = UserSimulator(SessionLocal)
    sim.reset_user_data(config.phone)
    st.session_state.user_simulator_initialized = False
    st.session_state.pop("reset_db_requested", None)
    st.toast("Base de datos reseteada")

# Handle user simulation setup
if config.user_registered:
    if not st.session_state.user_simulator_initialized:
        from app.models.database import SessionLocal
        sim = UserSimulator(SessionLocal)
        sim.create_test_user(config.phone, config.user_name)
        st.session_state.user_simulator_initialized = True
else:
    # Desvincular el usuario de test para que el flujo sea el de no registrado.
    from app.models.database import SessionLocal
    sim = UserSimulator(SessionLocal)
    sim.delete_test_user(config.phone)
    st.session_state.user_simulator_initialized = False

# Render chat
render_chat(config)
