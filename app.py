"""Landing page — mailbox status dashboard."""
import streamlit as st
from core.mongo import get_all_statuses, get_accounts, ensure_indexes

st.set_page_config(
    page_title="Auto-Reply Manager",
    page_icon="📬",
    layout="wide",
)

try:
    ensure_indexes()
except Exception:
    pass

st.title("📬 Auto-Reply Classification & Enrichment")
st.caption("Vedsu Technologies — B2B Healthcare Outreach")

statuses  = get_all_statuses()
accounts  = get_accounts()
acct_set  = {a["email"] for a in accounts}

STATUS_COLORS = {
    "synced":      "🔵",
    "classifying": "🟡",
    "classified":  "🟢",
    "reviewing":   "🟠",
    "archived":    "✅",
}

# ── Summary tiles ──────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns(4)
c1.metric("Saved Accounts",   len(accounts))
c2.metric("Mailboxes Synced", sum(1 for s in statuses if s.get("status")))
c3.metric("Classified",       sum(1 for s in statuses if s.get("status") in ("classified","reviewing","archived")))
c4.metric("Archived",         sum(1 for s in statuses if s.get("archived")))

st.divider()

# ── Per-mailbox table ───────────────────────────────────────────────────────
st.subheader("Mailbox Status")

if not statuses and not accounts:
    st.info("No accounts saved yet. Go to **1 Accounts** to add mailboxes.")
else:
    def _n(v):
        return "—" if v is None else str(v)

    rows = []
    # Show status rows first
    for s in statuses:
        email   = s.get("email", "")
        status  = s.get("status", "—")
        counts  = s.get("classified_counts", {})
        rows.append({
            "":              STATUS_COLORS.get(status, "⚪"),
            "Email":         email,
            "Status":        status,
            "Synced":        _n(s.get("synced_count")),
            "Removal":       _n(counts.get("removal")),
            "Unavailable":   _n(counts.get("unavailable")),
            "Prospect":      _n(counts.get("prospect")),
            "Redirects":     _n(counts.get("redirects")),
            "Last Sync":     s["last_synced_at"].strftime("%Y-%m-%d") if s.get("last_synced_at") else "—",
            "Archived":      "✅" if s.get("archived") else "",
        })
    # Accounts with no status yet
    tracked = {s["email"] for s in statuses}
    for a in accounts:
        if a["email"] not in tracked:
            rows.append({
                "":            "⚪",
                "Email":       a["email"],
                "Status":      "not synced",
                "Synced":      "—",
                "Removal":     "—",
                "Unavailable": "—",
                "Prospect":    "—",
                "Redirects":   "—",
                "Last Sync":   "—",
                "Archived":    "",
            })

    import pandas as pd
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

st.divider()
st.caption("Navigate using the sidebar: Accounts → Sync → Classify → Review → Archive")
