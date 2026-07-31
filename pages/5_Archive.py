"""Page 5 — S3 archive + two-step staging delete."""
from datetime import datetime, timezone

import streamlit as st
from core.auth import require_login

from core.mongo import (
    get_accounts, get_all_statuses, get_status, upsert_status,
    staging_to_dataframe, staging_count, drop_staging, count_prospects,
)
from core.s3_archive import upload_csv, verify_upload, s3_configured

st.set_page_config(page_title="Archive", page_icon="🗄️", layout="wide")
require_login()
st.title("🗄️ Archive & Delete Staging")

accounts = get_accounts()
statuses = {s["email"]: s for s in get_all_statuses()}

if not accounts:
    st.warning("No accounts configured.")
    st.stop()

# Only show mailboxes that are classified (all prospects reviewed or explicitly ready)
eligible = []
for a in accounts:
    s = statuses.get(a["email"], {})
    if s.get("status") in ("classified", "reviewing") and not s.get("archived"):
        unchecked = count_prospects(a["email"], unchecked_only=True)
        eligible.append((a, s, unchecked))

if not eligible:
    st.info("No mailboxes ready for archiving. Complete classification (**3 Classify**) first.")
    st.stop()

# ── Mailbox selector ──────────────────────────────────────────────────────────
selected_email = st.selectbox(
    "Select Mailbox to Archive",
    [a["email"] for a, _, _ in eligible],
)
acc, s, unchecked = next((a, s, u) for a, s, u in eligible if a["email"] == selected_email)

# ── Status summary ────────────────────────────────────────────────────────────
with st.container(border=True):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Staging Docs",      staging_count(acc["email"]))
    cc = s.get("classified_counts", {})
    c2.metric("Removal",           cc.get("removal",0))
    c3.metric("Unavailable",       cc.get("unavailable",0))
    c4.metric("Prospects",         cc.get("prospect",0) + cc.get("redirects",0))

if unchecked > 0:
    st.warning(
        f"⚠️ **{unchecked}** prospect(s) not yet reviewed. "
        "You can archive anyway, but unreviewed prospects will remain in the `prospect` collection — "
        "they are NOT deleted with staging."
    )

# ── AWS check ─────────────────────────────────────────────────────────────────
st.divider()
if not s3_configured():
    st.error(
        "AWS credentials not configured. "
        "Add `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_BUCKET`, and `AWS_REGION` "
        "to `.streamlit/secrets.toml`."
    )
    st.stop()

# ── Step 1: Export + Upload ───────────────────────────────────────────────────
st.subheader("Step 1 — Export staging to CSV and upload to S3")

if st.button("📤 Export & Upload to S3", width='stretch'):
    with st.spinner("Reading staging collection…"):
        df = staging_to_dataframe(acc["email"])

    if df.empty:
        st.warning("Staging collection is empty — nothing to export.")
        st.stop()

    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    st.caption(f"CSV size: {len(csv_bytes):,} bytes · {len(df):,} rows")

    with st.spinner("Uploading to S3…"):
        ok, key, err = upload_csv(acc["email"], csv_bytes)

    if not ok:
        st.error(f"Upload failed: {err}")
    else:
        st.success(f"Uploaded ✓  →  `{key}`")
        st.session_state["archive_key"]   = key
        st.session_state["archive_email"] = acc["email"]
        st.session_state["archive_rows"]  = len(df)

# ── Step 2: Verify ────────────────────────────────────────────────────────────
if st.session_state.get("archive_key") and st.session_state.get("archive_email") == acc["email"]:
    key      = st.session_state["archive_key"]
    exp_rows = st.session_state.get("archive_rows", 0)

    st.divider()
    st.subheader("Step 2 — Verify upload")

    if st.button("🔎 Verify S3 Object", width='stretch'):
        with st.spinner("Verifying…"):
            v = verify_upload(key, exp_rows)
        st.session_state["archive_verified"] = v["ok"]
        if v["ok"]:
            st.success(f"Verification passed ✓  —  {v['message']}")
        else:
            st.error(f"Verification FAILED: {v['message']}")

    # ── Step 3: Delete (only after verified) ──────────────────────────────────
    if st.session_state.get("archive_verified"):
        st.divider()
        st.subheader("Step 3 — Delete staging collection")
        st.error(
            f"⚠️ This will **permanently delete** the staging collection "
            f"`{acc['email']}` and its sync metadata from MongoDB. "
            "The classified data (removal / unavailable / prospect collections) is kept."
        )

        confirmed = st.checkbox(
            f"I confirm: delete staging collection for **{acc['email']}** "
            f"(S3 backup at `{key}`)"
        )

        if confirmed:
            if st.button("🗑️ Delete Staging Now", type="primary", width='stretch'):
                with st.spinner("Deleting…"):
                    drop_staging(acc["email"])
                    upsert_status(acc["email"], {
                        "archived":      True,
                        "archive_s3_key": key,
                        "archived_at":   datetime.now(timezone.utc),
                        "status":        "archived",
                        "last_error":    None,
                    })

                # Clear session flags
                for k in ("archive_key", "archive_email", "archive_rows", "archive_verified"):
                    st.session_state.pop(k, None)

                st.success(
                    f"✅ Staging deleted. Mailbox **{acc['email']}** is now archived. "
                    f"S3 key: `{key}`"
                )
                st.balloons()
