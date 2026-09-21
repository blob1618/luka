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

# Testing environment always uses an isolated SQLite database.  Containers may
# place it in a managed volume so Windows bind-mount permissions cannot make it
# read-only.
testing_database_url = os.getenv(
    "TESTING_DATABASE_URL", "sqlite:///./testing_luka.db"
)
os.environ["DATABASE_URL"] = testing_database_url

from app.models.database import SessionLocal, engine  # noqa: E402
from testing.services.schema import ensure_testing_schema  # noqa: E402

ensure_testing_schema(engine)

import streamlit as st  # noqa: E402

from testing.config.settings import TestingConfig, new_session  # noqa: E402
from testing.components.sidebar import render_sidebar  # noqa: E402
from testing.components.chat import render_chat  # noqa: E402
from testing.services.user_simulator import (  # noqa: E402
    UserSimulator,
    sync_test_user,
)

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
if "config" not in st.session_state:
    st.session_state.config = TestingConfig()

config = st.session_state.config

if not config.sessions:
    session = new_session(
        label="Sesión 1",
        phone="5491112345678",
        user_name="Test User",
        user_registered=True,
    )
    config.sessions.append(session)
    config.active_session_id = session.id
    sync_test_user(
        SessionLocal,
        phone=session.phone,
        name=session.user_name,
        registered=session.user_registered,
    )

# Render sidebar and get config
config = render_sidebar()

# Handle DB reset
if st.session_state.get("reset_db_requested"):
    active = config.active_session()
    if active is not None:
        UserSimulator(SessionLocal).reset_user_data(active.phone)
    st.session_state.pop("reset_db_requested", None)
    st.toast("Base de datos reseteada")

# Render chat
render_chat(config)
