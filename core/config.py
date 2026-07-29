"""Load secrets from st.secrets (cloud) or .env file (local dev)."""
import os
from dotenv import load_dotenv

load_dotenv()


def get_secret(key: str, default: str = "") -> str:
    try:
        import streamlit as st
        return st.secrets.get(key, default)
    except Exception:
        return os.environ.get(key, default)
