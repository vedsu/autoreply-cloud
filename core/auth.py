"""Auth: plain-password login with persistent session token stored in query params."""
import secrets
from datetime import datetime, timezone, timedelta

COL_USERS    = "users"
COL_SESSIONS = "sessions"
SESSION_DAYS = 30


def _make_token() -> str:
    return secrets.token_urlsafe(32)


def _create_session(db, username: str) -> str:
    token = _make_token()
    db[COL_SESSIONS].insert_one({
        "token":      token,
        "username":   username,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
    })
    return token


def _validate_token(db, token: str):
    """Return session doc if token is valid and not expired, else None."""
    return db[COL_SESSIONS].find_one({
        "token":      token,
        "expires_at": {"$gt": datetime.now(timezone.utc)},
    })


def _logout_ui(st):
    with st.sidebar:
        st.divider()
        st.caption(f"👤 {st.session_state.get('username', '')}")
        if st.button("🚪 Logout", key="logout_btn_sidebar"):
            token = st.session_state.get("token") or st.query_params.get("token")
            if token:
                from core.mongo import db as _db
                _db()[COL_SESSIONS].delete_one({"token": token})
            st.session_state.clear()
            st.query_params.clear()
            st.rerun()


def require_login():
    """
    Call right after st.set_page_config() on every page.
    Login persists across browser reloads via a session token in the URL query param.
    """
    import streamlit as st
    from core.mongo import db

    # ── Already authenticated this session ────────────────────────────────────
    if st.session_state.get("authenticated"):
        _logout_ui(st)
        return

    # ── Restore from persistent token in URL ─────────────────────────────────
    token = st.query_params.get("token")
    if token:
        session = _validate_token(db(), token)
        if session:
            st.session_state["authenticated"] = True
            st.session_state["username"]      = session["username"]
            st.session_state["token"]         = token
            _logout_ui(st)
            return
        else:
            # Token expired or invalid — clear it
            st.query_params.clear()

    # ── First-time setup (no users in DB) ────────────────────────────────────
    user_count = db()[COL_USERS].count_documents({})

    if user_count == 0:
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
                else:
                    db()[COL_USERS].insert_one({
                        "username":   new_user.lower().strip(),
                        "password":   new_pass,
                        "role":       "admin",
                        "created_at": datetime.now(timezone.utc),
                    })
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
                user = db()[COL_USERS].find_one({"username": username.lower().strip()})
                if user and user.get("password") == password:
                    token = _create_session(db(), user["username"])
                    st.session_state["authenticated"] = True
                    st.session_state["username"]      = user["username"]
                    st.session_state["token"]         = token
                    st.query_params["token"]          = token
                    st.rerun()
                else:
                    st.error("Invalid username or password.")

    st.stop()
