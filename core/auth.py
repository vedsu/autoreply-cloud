"""Simple username/password auth backed by MongoDB. No extra dependencies."""
import hashlib
import os
from datetime import datetime, timezone

import streamlit as st

from core.mongo import db

COL_USERS = "users"


def _hash(password: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + key.hex()


def _verify(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":")
        key = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 100_000
        )
        return key.hex() == key_hex
    except Exception:
        return False


def _user_count() -> int:
    return db()[COL_USERS].count_documents({})


def _find_user(username: str):
    return db()[COL_USERS].find_one({"username": username.lower().strip()})


def _create_user(username: str, password: str, role: str = "admin"):
    db()[COL_USERS].insert_one({
        "username":      username.lower().strip(),
        "password_hash": _hash(password),
        "role":          role,
        "created_at":    datetime.now(timezone.utc),
    })


def require_login():
    """
    Call right after st.set_page_config() on every page.
    - If no users exist: shows first-time setup form.
    - If not authenticated: shows login form.
    - If authenticated: injects logout button into sidebar and returns.
    """
    if st.session_state.get("authenticated"):
        # Inject logout into sidebar on every authenticated page
        with st.sidebar:
            st.divider()
            st.caption(f"👤 {st.session_state.get('username', '')}")
            if st.button("🚪 Logout", key="_logout_btn", width="stretch"):
                st.session_state.clear()
                st.rerun()
        return

    # ── First-time setup ──────────────────────────────────────────────────────
    if _user_count() == 0:
        st.title("🔐 Create Admin Account")
        st.info("No users found. Set up your admin credentials to get started.")

        with st.form("setup_form"):
            new_user = st.text_input("Username")
            new_pass = st.text_input("Password", type="password")
            new_conf = st.text_input("Confirm Password", type="password")
            if st.form_submit_button("Create Account", type="primary", width="stretch"):
                if not new_user.strip() or not new_pass:
                    st.error("Username and password are required.")
                elif new_pass != new_conf:
                    st.error("Passwords do not match.")
                elif len(new_pass) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    _create_user(new_user, new_pass)
                    st.success("Account created — please log in.")
                    st.rerun()

        st.stop()
        return

    # ── Login form ────────────────────────────────────────────────────────────
    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown("## 🔐 Login")
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Login", type="primary", width="stretch"):
                user = _find_user(username)
                if user and _verify(password, user["password_hash"]):
                    st.session_state["authenticated"] = True
                    st.session_state["username"] = user["username"]
                    st.session_state["role"] = user.get("role", "user")
                    st.rerun()
                else:
                    st.error("Invalid username or password.")

    st.stop()
