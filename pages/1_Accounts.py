"""Page 1 — IMAP account management (stored in MongoDB)."""
import streamlit as st
from core.mongo import get_accounts, upsert_account, delete_account
from core.imap_sync import test_connection, classify_error

PORT_OPTIONS = {
    "993 (IMAP SSL)": 993,
    "143 (IMAP)":     143,
}

st.set_page_config(page_title="Accounts", page_icon="🔑", layout="wide")
st.title("🔑 IMAP Accounts")

# ── Add / Update ────────────────────────────────────────────────────────────
with st.expander("➕ Add / Update Account", expanded=True):
    with st.form("add_account"):
        c1, c2 = st.columns(2)
        new_email  = c1.text_input("Email")
        new_pass   = c2.text_input("Password", type="password")
        c3, c4     = st.columns(2)
        new_server = c3.text_input("IMAP Server", placeholder="mail.domain.com")
        port_label = c4.selectbox("Port", list(PORT_OPTIONS))
        submitted  = st.form_submit_button("Test & Save", width='stretch', type="primary")

    if submitted:
        if not all([new_email, new_pass, new_server]):
            st.error("All fields are required.")
        else:
            port = PORT_OPTIONS[port_label]
            with st.status(f"Testing {new_server}:{port}…", expanded=True) as s:
                try:
                    info = test_connection(new_email.strip(), new_pass, new_server.strip(), port)
                    s.update(label="Connection successful ✓", state="complete")
                    st.success(f"Inbox: {info.get('inbox_total',0)} emails")
                    upsert_account(new_email.strip(), new_pass, new_server.strip(), port)
                    st.success(f"Saved: {new_email.strip()}")
                    st.rerun()
                except Exception as e:
                    s.update(label="Connection failed ✗", state="error")
                    st.error(classify_error(e))
                    st.warning("Account NOT saved — fix the issue and try again.")

# ── Saved accounts ──────────────────────────────────────────────────────────
st.divider()
accounts = get_accounts()
st.subheader(f"Saved Accounts ({len(accounts)})")

if not accounts:
    st.info("No accounts yet.")
else:
    for acc in accounts:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
            c1.markdown(f"**{acc['email']}**")
            c2.caption(f"{acc['server']}:{acc['port']}")

            if c3.button("Test", key=f"test_{acc['email']}"):
                with st.spinner("Testing…"):
                    try:
                        info = test_connection(acc["email"], acc["password"], acc["server"], acc["port"])
                        st.success(f"✓ Inbox: {info.get('inbox_total',0)}")
                    except Exception as e:
                        st.error(classify_error(e))

            if c4.button("Remove", key=f"rm_{acc['email']}"):
                delete_account(acc["email"])
                st.rerun()
