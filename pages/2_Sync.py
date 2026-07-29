"""Page 2 — IMAP sync for a single mailbox."""
from datetime import date, timedelta, datetime

import streamlit as st

from core.mongo import (
    get_accounts, upsert_status, upsert_sync_meta,
    staging_col_name, db, staging_count,
)
from core.imap_sync import fetch_all, test_connection, classify_error

st.set_page_config(page_title="Sync", page_icon="📥", layout="wide")
st.title("📥 Sync Mailbox")

accounts = get_accounts()
if not accounts:
    st.warning("No accounts saved. Go to **1 Accounts** first.")
    st.stop()

# ── Account selection ────────────────────────────────────────────────────────
emails         = [a["email"] for a in accounts]
selected_email = st.selectbox("Select Mailbox", emails)
acc            = next(a for a in accounts if a["email"] == selected_email)

with st.container(border=True):
    c1, c2, c3 = st.columns(3)
    c1.text_input("Email",  value=acc["email"],  disabled=True)
    c2.text_input("Server", value=acc["server"], disabled=True)
    c3.text_input("Port",   value=str(acc["port"]), disabled=True)

# ── Connection test ──────────────────────────────────────────────────────────
if st.button("Test Connection", width='stretch'):
    with st.spinner(f"Testing {acc['server']}:{acc['port']}…"):
        try:
            info = test_connection(acc["email"], acc["password"], acc["server"], acc["port"])
            st.session_state["conn_info"] = info
        except Exception as e:
            st.session_state.pop("conn_info", None)
            st.error(classify_error(e))

if "conn_info" in st.session_state:
    info = st.session_state["conn_info"]
    st.success("Connected ✓")
    cc1, cc2 = st.columns(2)
    cc1.metric("Inbox",  info.get("inbox_total", 0))
    cc2.metric("Spam",   info.get("spam_total",  0))
    st.caption(f"Earliest inbox: {info.get('inbox_earliest',{}).get('date','—')}  |  Spam folder: {info.get('spam_folder') or 'not detected'}")

# ── Fetch settings ────────────────────────────────────────────────────────────
st.divider()
st.subheader("Fetch Settings")
with st.container(border=True):
    f1, f2, f3, f4 = st.columns(4)
    start_date   = f1.date_input("From Date",  value=date.today() - timedelta(days=1))
    end_date     = f2.date_input("To Date",    value=date.today())
    max_emails   = f3.number_input("Max Emails / Folder", 100, 50_000, 2_000, 500)
    body_limit   = f4.number_input("Body Bytes Limit",    0, 200_000, 50_000, 10_000,
                                   help="0 = headers only")
    st.caption(f"Range: {start_date} → {end_date}  |  Reconnect every 100 emails")

# ── Existing staging warning ──────────────────────────────────────────────────
existing = staging_count(acc["email"])
if existing:
    st.warning(f"⚠️ Staging collection already has **{existing:,}** docs. "
               "A new sync will ADD to them (same message_id is safe — upserted).")

# ── Fetch ─────────────────────────────────────────────────────────────────────
if st.button("🚀 Fetch Emails", type="primary", width='stretch'):
    if start_date > end_date:
        st.error("Start date must be before end date.")
        st.stop()
    if end_date > date.today():
        st.error("End date cannot be in the future.")
        st.stop()

    progress_bar = st.progress(0)
    status_box   = st.empty()

    def on_progress(pct):
        progress_bar.progress(int(pct * 100))

    def on_status(msg):
        status_box.info(msg)

    try:
        result = fetch_all(
            email_id      = acc["email"],
            password      = acc["password"],
            host          = acc["server"],
            port          = acc["port"],
            timeout       = 60,
            start         = start_date,
            end           = end_date,
            max_emails    = int(max_emails),
            body_limit    = int(body_limit),
            reconnect_every = 100,
            on_progress   = on_progress,
            on_status     = on_status,
        )

        emails_fetched = result["emails"]
        meta           = result["sync_metadata"]

        # Save to staging collection
        col = db()[staging_col_name(acc["email"])]
        if emails_fetched:
            inserted = 0
            for doc in emails_fetched:
                doc["_account_email"] = acc["email"]
                op = col.update_one(
                    {"message_id": doc["message_id"]} if doc.get("message_id") else {"sequence_id": doc["sequence_id"]},
                    {"$set": doc},
                    upsert=True,
                )
                if op.upserted_id:
                    inserted += 1

        # Save sync meta
        upsert_sync_meta(acc["email"], {**meta, "generated_at": datetime.utcnow().isoformat()})

        # Update mailbox processing status
        total_in_staging = staging_count(acc["email"])
        upsert_status(acc["email"], {
            "status":          "synced",
            "synced_count":    total_in_staging,
            "last_synced_at":  datetime.utcnow(),
            "last_error":      None,
        })

        progress_bar.progress(100)
        status_box.success("Sync complete ✓")

        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Fetched",       meta.get("total_fetched", 0))
        r2.metric("Inbox",         meta.get("inbox_fetched", 0))
        r3.metric("Spam",          meta.get("spam_fetched",  0))
        r4.metric("Skipped",       meta.get("skipped",       0))
        st.caption(f"Total in staging now: **{total_in_staging:,}**")

    except Exception as e:
        upsert_status(acc["email"], {"last_error": str(e)})
        st.error(classify_error(e))
