"""Page 3 — Gemini batch classification of a synced mailbox."""
from datetime import datetime, timezone

import streamlit as st

from core.mongo import (
    get_accounts, get_all_statuses, upsert_status,
    iter_staging, staging_count, staging_to_dataframe, drop_staging,
    mark_staging_classified,
)
from core.classify import classify_batch, process_classification_results
from core.s3_archive import upload_csv, verify_upload, s3_configured

BATCH_SIZE = 25

st.set_page_config(page_title="Classify", page_icon="🧠", layout="wide")
st.title("🧠 Classify Mailbox")

accounts = get_accounts()
statuses = {s["email"]: s for s in get_all_statuses()}

if not accounts:
    st.warning("No accounts. Go to **1 Accounts** first.")
    st.stop()

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

# ── Status summary ────────────────────────────────────────────────────────────
s                  = statuses.get(selected_email, {})
total_in_staging   = staging_count(selected_email)                    # all docs
remaining_staging  = staging_count(selected_email, only_unclassified=True)  # not yet done
already_done       = total_in_staging - remaining_staging

with st.container(border=True):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total in Staging",    total_in_staging)
    c2.metric("Already Classified",  already_done)
    c3.metric("Remaining",           remaining_staging)
    c4.metric("Current Status",      s.get("status", "—"))

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

if remaining_staging == 0:
    st.success("✅ All emails in staging are already classified. You can archive the staging data.")
    st.stop()

# ── Run settings ─────────────────────────────────────────────────────────────
st.divider()
with st.container(border=True):
    rc1, rc2 = st.columns([2, 3])
    with rc1:
        max_run = st.number_input(
            "Max emails this run (0 = all remaining)",
            min_value=0, max_value=50_000,
            value=0, step=100,
            help="Useful on Streamlit Cloud where long runs time out. "
                 "Classified emails are tracked so the next run picks up where this left off.",
        )
    with rc2:
        effective = remaining_staging if max_run == 0 else min(max_run, remaining_staging)
        st.metric("Will classify", effective,
                  delta=f"{remaining_staging - effective} deferred" if effective < remaining_staging else None)

# ── Classify ──────────────────────────────────────────────────────────────────
if st.button("▶ Start Classification", type="primary", width='stretch'):
    upsert_status(selected_email, {"status": "classifying", "last_error": None})

    progress_bar = st.progress(0)
    status_box   = st.empty()
    totals = {"removal": 0, "unavailable": 0, "prospect": 0, "redirects": 0, "errors": 0}
    provider_stats = {"gemini": 0, "deepseek": 0}
    metrics_box  = st.empty()

    batch_num  = 0
    total_done = 0
    run_limit  = max_run if max_run > 0 else remaining_staging

    for batch in iter_staging(selected_email, batch_size=BATCH_SIZE):
        # Trim batch if it would exceed the run limit
        if total_done + len(batch) > run_limit:
            batch = batch[:run_limit - total_done]
        if not batch:
            break

        batch_num += 1
        status_box.info(
            f"Classifying batch {batch_num} "
            f"({total_done + len(batch)}/{run_limit})…"
        )

        try:
            results = classify_batch(batch, _stats=provider_stats)
            counts  = process_classification_results(results, batch, selected_email)
            for k in totals:
                totals[k] += counts.get(k, 0)

            # Mark successfully classified docs so restarts skip them
            classified_mids = [r.get("message_id") for r in results if r.get("message_id")]
            mark_staging_classified(selected_email, classified_mids)

            # Emails silently omitted stay unclassified → retried next run
            missed = len(batch) - len(results)
            if missed > 0:
                totals["errors"] += missed

        except Exception as e:
            totals["errors"] += len(batch)
            status_box.warning(f"Batch {batch_num} failed: {e}")

        total_done += len(batch)
        progress_bar.progress(min(int(total_done / run_limit * 100), 100))

        metrics_box.markdown(
            f"**Removal:** {totals['removal']}  |  "
            f"**Unavailable:** {totals['unavailable']}  |  "
            f"**Prospect:** {totals['prospect']}  |  "
            f"**Redirects:** {totals['redirects']}  |  "
            f"**Errors:** {totals['errors']}  |  "
            f"🟢 Gemini: {provider_stats['gemini']} batches  "
            f"🔵 DeepSeek: {provider_stats['deepseek']} batches"
        )

        if total_done >= run_limit:
            break

    # Finalize
    upsert_status(selected_email, {
        "status":            "classified",
        "classified_counts": totals,
        "classified_at":     datetime.now(timezone.utc),
        "last_error":        None if totals["errors"] == 0 else f"{totals['errors']} emails missed",
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

    p1, p2 = st.columns(2)
    p1.metric("🟢 Gemini batches",   provider_stats["gemini"])
    p2.metric("🔵 DeepSeek batches", provider_stats["deepseek"])

    still_remaining = staging_count(selected_email, only_unclassified=True)
    if still_remaining > 0 and total_done >= run_limit and max_run > 0:
        st.info(f"📋 Run limit reached. **{still_remaining}** emails still remaining — run again to continue.")
    elif totals["errors"] > 0:
        st.warning(f"{totals['errors']} emails could not be classified and will be retried on the next run.")
    else:
        # Zero errors — auto S3 archive
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
                            "archived_at":    datetime.now(timezone.utc),
                            "status":         "archived",
                        })
                        archive_box.success(f"✅ Staging auto-archived → S3 key: `{key}`")
                    else:
                        archive_box.warning(f"S3 verify failed ({v['message']}) — staging kept. Archive manually.")
                else:
                    archive_box.warning(f"S3 upload failed ({err}) — staging kept. Archive manually.")
            except Exception as e:
                st.warning(f"Auto-archive error: {e} — go to **5 Archive** to archive manually.")
        else:
            st.info("S3 not configured — go to **5 Archive** to archive staging manually.")
