"""Page 3 — Gemini batch classification of a synced mailbox."""
from datetime import datetime

import streamlit as st

from core.mongo import (
    get_accounts, get_all_statuses, upsert_status,
    iter_staging, staging_count, staging_to_dataframe, drop_staging,
    get_staging_message_ids,
)
from core.classify import classify_batch, process_classification_results
from core.s3_archive import upload_csv, verify_upload, s3_configured
from core.imap_sync import delete_inbox_emails

BATCH_SIZE = 25

st.set_page_config(page_title="Classify", page_icon="🧠", layout="wide")
st.title("🧠 Classify Mailbox")

accounts   = get_accounts()
statuses   = {s["email"]: s for s in get_all_statuses()}

if not accounts:
    st.warning("No accounts. Go to **1 Accounts** first.")
    st.stop()

# All statuses except archived are eligible — re-run is always safe (records upsert)
eligible = [
    a for a in accounts
    if statuses.get(a["email"], {}).get("status") in
       ("synced", "classifying", "classified", "reviewing", "archived")
    and staging_count(a["email"]) > 0
]

if not eligible:
    st.info("No mailboxes ready for classification. Sync a mailbox first (**2 Sync**).")
    st.stop()

selected_email = st.selectbox(
    "Select Mailbox to Classify",
    [a["email"] for a in eligible],
)

# ── Status summary ───────────────────────────────────────────────────────────
s = statuses.get(selected_email, {})
total_in_staging = staging_count(selected_email)

with st.container(border=True):
    c1, c2, c3 = st.columns(3)
    c1.metric("Emails in Staging", total_in_staging)
    c2.metric("Batches (~25/call)", max(1, total_in_staging // BATCH_SIZE))
    c3.metric("Current Status", s.get("status", "—"))

if s.get("classified_counts"):
    cc = s["classified_counts"]
    st.caption(
        f"Previous run — Removal: {cc.get('removal',0)}  |  "
        f"Unavailable: {cc.get('unavailable',0)}  |  "
        f"Prospect: {cc.get('prospect',0)}  |  "
        f"Redirects: {cc.get('redirects',0)}"
    )

if total_in_staging == 0:
    st.warning("Staging collection is empty. Sync first.")
    st.stop()

# ── Manual inbox clean — available whenever staging has data ─────────────────
if total_in_staging > 0:
    with st.expander("🧹 Clean Inbox (move classified emails to archive folder)"):
        st.caption(
            f"{total_in_staging} staged message IDs available. "
            "Connects via IMAP and moves those emails from INBOX/Spam to the archive folder."
        )
        if st.button("🧹 Clean Inbox Now", width='stretch'):
            acc = next((a for a in accounts if a["email"] == selected_email), None)
            if not acc:
                st.error("Account credentials not found.")
            else:
                msg_ids = get_staging_message_ids(selected_email)
                if not msg_ids:
                    st.warning("No message IDs in staging.")
                else:
                    with st.spinner(f"Moving {len(msg_ids)} emails to archive folder…"):
                        result = delete_inbox_emails(
                            email_id=acc["email"],
                            password=acc["password"],
                            host=acc["server"],
                            port=acc["port"],
                            message_ids=msg_ids,
                        )
                    if result.get("error"):
                        st.error(f"Error: {result['error']}")
                    else:
                        st.success(
                            f"✅ Done — {result['deleted']} moved to Trash, "
                            f"{result['not_found']} not found, {result['errors']} errors."
                        )

# ── Classify ─────────────────────────────────────────────────────────────────
st.divider()
if st.button("▶ Start Classification", type="primary", width='stretch'):
    upsert_status(selected_email, {"status": "classifying", "last_error": None})

    progress_bar  = st.progress(0)
    status_box    = st.empty()
    totals = {"removal": 0, "unavailable": 0, "prospect": 0, "redirects": 0, "errors": 0}
    metrics_box   = st.empty()

    batch_num   = 0
    total_done  = 0

    for batch in iter_staging(selected_email, batch_size=BATCH_SIZE):
        batch_num  += 1
        status_box.info(f"Classifying batch {batch_num} ({total_done + len(batch)}/{total_in_staging})…")

        try:
            results = classify_batch(batch)
            counts  = process_classification_results(results, batch, selected_email)
            for k in totals:
                totals[k] += counts.get(k, 0)
            # Any emails Gemini silently omitted from its JSON response
            missed = len(batch) - len(results)
            if missed > 0:
                totals["errors"] += missed
        except Exception as e:
            totals["errors"] += len(batch)
            status_box.warning(f"Batch {batch_num} failed after retries: {e}")

        total_done += len(batch)
        progress_bar.progress(min(int(total_done / total_in_staging * 100), 100))

        metrics_box.markdown(
            f"**Removal:** {totals['removal']}  |  "
            f"**Unavailable:** {totals['unavailable']}  |  "
            f"**Prospect:** {totals['prospect']}  |  "
            f"**Redirects:** {totals['redirects']}  |  "
            f"**Errors:** {totals['errors']}"
        )

    # Finalize status
    upsert_status(selected_email, {
        "status":              "classified",
        "classified_counts":   totals,
        "classified_at":       datetime.utcnow(),
        "last_error":          None if totals["errors"] == 0 else f"{totals['errors']} batch errors",
    })

    progress_bar.progress(100)
    status_box.success("Classification complete ✓")

    st.subheader("Final Counts")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Removal",     totals["removal"])
    m2.metric("Unavailable", totals["unavailable"])
    m3.metric("Prospects",   totals["prospect"])
    m4.metric("Redirects",   totals["redirects"])
    m5.metric("Errors",      totals["errors"])

    if totals["errors"] > 0:
        st.warning(
            f"{totals['errors']} emails could not be classified. "
            "You can re-run classification — already-classified records are safely upserted."
        )
    else:
        # Zero errors — collect message IDs first, then auto-archive + clean inbox
        message_ids = get_staging_message_ids(selected_email)

        if s3_configured():
            archive_box = st.empty()
            archive_box.info("⏳ Auto-archiving staging data to S3…")
            try:
                df        = staging_to_dataframe(selected_email)
                csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
                ok, key, err = upload_csv(selected_email, csv_bytes)
                if ok:
                    v = verify_upload(key, len(df))
                    if v["ok"]:
                        drop_staging(selected_email)
                        upsert_status(selected_email, {
                            "archived":       True,
                            "archive_s3_key": key,
                            "archived_at":    datetime.utcnow(),
                            "status":         "archived",
                        })
                        archive_box.success(f"✅ Staging auto-archived → S3 key: `{key}`")
                    else:
                        archive_box.warning(f"S3 verify failed ({v['message']}) — staging kept. Archive manually.")
                        message_ids = []   # don't clean inbox if archive failed
                else:
                    archive_box.warning(f"S3 upload failed ({err}) — staging kept. Archive manually.")
                    message_ids = []
            except Exception as e:
                st.warning(f"Auto-archive error: {e} — go to **5 Archive** to archive manually.")
                message_ids = []
        else:
            st.info("S3 not configured — go to **5 Archive** to archive staging manually.")

        # Move classified emails to archive folder in IMAP inbox
        if message_ids:
            acc = next((a for a in accounts if a["email"] == selected_email), None)
            if acc:
                inbox_box = st.empty()
                inbox_box.info(f"⏳ Moving {len(message_ids)} emails to archive folder…")
                try:
                    result = delete_inbox_emails(
                        email_id=acc["email"],
                        password=acc["password"],
                        host=acc["server"],
                        port=acc["port"],
                        message_ids=message_ids,
                    )
                    if result.get("error"):
                        inbox_box.warning(f"Inbox archive: {result['error']}")
                    else:
                        inbox_box.success(
                            f"📬 Inbox cleaned — {result['deleted']} moved to Trash, "
                            f"{result['not_found']} not found, {result['errors']} errors."
                        )
                except Exception as e:
                    inbox_box.warning(f"Inbox archive error: {e}")
