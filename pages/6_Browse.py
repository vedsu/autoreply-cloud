"""Page 6 — Browse & export classified emails (removal, unavailable, prospects)."""
import streamlit as st
from core.auth import require_login
import pandas as pd

from core.mongo import (
    get_accounts, query_collection, export_prospects,
    COL_REMOVAL, COL_UNAVAIL,
)

st.set_page_config(page_title="Browse", page_icon="📋", layout="wide")
require_login()
st.title("📋 Browse & Export")

# ── Sidebar filters ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")

    collection_choice = st.radio(
        "Category",
        ["Prospects", "Removal", "Unavailable"],
        horizontal=False,
    )

    accounts = get_accounts()
    mailbox_opts = ["All Mailboxes"] + [a["email"] for a in accounts]
    selected_mailbox = st.selectbox("Mailbox", mailbox_opts)
    mailbox_filter = "" if selected_mailbox == "All Mailboxes" else selected_mailbox

    st.divider()

    # ── Prospect-specific filters ─────────────────────────────────────────────
    if collection_choice == "Prospects":
        _INDUSTRIES = ["Healthcare", "Pharmaceuticals", "Finance",
                       "Human Resources", "Education", "Other"]

        status_choice = st.radio(
            "Status",
            ["Confirmed", "Skipped", "All"],
            horizontal=True,
            help="Confirmed = reviewed & accepted · Skipped = needs follow-up",
        )
        industry_filter = st.selectbox("Industry", ["All Industries"] + _INDUSTRIES)
        industry_val = "" if industry_filter == "All Industries" else industry_filter

        job_title_filter = st.text_input("Job Title keyword",
                                         placeholder="e.g. Director, HR Manager")

        include_generic = st.checkbox("Include generic/irrelevant", value=False)
        limit = st.slider("Max rows", 100, 50_000, 1000, 100)
        search_term = st.text_input("Search email / name / company",
                                    placeholder="e.g. @hospital.com")

    # ── Removal / Unavailable filters ─────────────────────────────────────────
    else:
        col_name = COL_REMOVAL if collection_choice == "Removal" else COL_UNAVAIL

        if collection_choice == "Removal":
            type_opts = {
                "All Types":   "",
                "Hard Bounce": "bounce_hard",
                "Soft Bounce": "bounce_soft",
                "Opt-Out":     "opt_out",
                "Inactive":    "inactive",
                "Departed":    "departed",
                "Deceased":    "deceased",
            }
        else:
            type_opts = {
                "All Types":    "",
                "Out of Office":"out_of_office",
                "Vacation":     "vacation",
                "On Leave":     "on_leave",
                "Medical Leave":"medical_leave",
            }

        type_label  = st.selectbox("Type", list(type_opts.keys()))
        type_filter = type_opts[type_label]
        limit       = st.slider("Max rows", 100, 2000, 500, 100)
        search_term = st.text_input("Search email / name",
                                    placeholder="e.g. john@company.com")

# ── Load data ─────────────────────────────────────────────────────────────────
with st.spinner("Loading…"):
    if collection_choice == "Prospects":
        rows = export_prospects(
            mailbox=mailbox_filter,
            industry=industry_val,
            job_title=job_title_filter.strip(),
            status=status_choice.lower(),
            include_generic=include_generic,
            limit=limit,
        )
    else:
        rows = query_collection(col_name, mailbox_filter, type_filter, limit)

if not rows:
    st.info("No records match the selected filters.")
    st.stop()

# ── Build DataFrame ────────────────────────────────────────────────────────────
df = pd.DataFrame(rows)

if collection_choice == "Prospects":
    # Flatten source_mailboxes array → string
    if "source_mailboxes" in df.columns:
        df["source_mailboxes"] = df["source_mailboxes"].apply(
            lambda v: "; ".join(v) if isinstance(v, list) else (v or "")
        )

    keep = ["email", "prospect_name", "job_title", "company", "industry",
            "webinar", "phone", "source_type", "source_mailbox",
            "original_sender_email", "classified_at"]
    for c in keep:
        if c not in df.columns:
            df[c] = ""
    df = df[keep].copy()
    df.columns = ["Email", "Name", "Job Title", "Company", "Industry",
                  "Webinar", "Phone", "Source Type", "Mailbox",
                  "Via (redirect from)", "Classified At"]
    df["Source Type"] = df["Source Type"].str.replace("_", " ").str.title()

    col_config = {
        "Email":             st.column_config.TextColumn(width="medium"),
        "Name":              st.column_config.TextColumn(width="small"),
        "Job Title":         st.column_config.TextColumn(width="medium"),
        "Company":           st.column_config.TextColumn(width="medium"),
        "Industry":          st.column_config.TextColumn(width="small"),
        "Webinar":           st.column_config.TextColumn(width="large"),
        "Phone":             st.column_config.TextColumn(width="small"),
        "Source Type":       st.column_config.TextColumn(width="small"),
        "Mailbox":           st.column_config.TextColumn(width="medium"),
        "Via (redirect from)":st.column_config.TextColumn(width="medium"),
        "Classified At":     st.column_config.TextColumn(width="small"),
    }

else:
    type_col = "removal_type" if collection_choice == "Removal" else "unavailable_type"
    keep = ["email", "name", type_col, "webinar", "source_mailbox", "classified_at"]
    for c in keep:
        if c not in df.columns:
            df[c] = ""
    df = df[keep].copy()
    df.columns = ["Email", "Name", "Type", "Webinar", "Mailbox", "Classified At"]

    type_labels = {
        "bounce_hard":   "Hard Bounce",  "bounce_soft":  "Soft Bounce",
        "opt_out":       "Opt-Out",      "inactive":     "Inactive",
        "departed":      "Departed",     "deceased":     "Deceased",
        "out_of_office": "Out of Office","vacation":     "Vacation",
        "on_leave":      "On Leave",     "medical_leave":"Medical Leave",
    }
    df["Type"] = df["Type"].map(lambda v: type_labels.get(v, v or "—"))
    col_config = {
        "Email":  st.column_config.TextColumn(width="medium"),
        "Name":   st.column_config.TextColumn(width="small"),
        "Type":   st.column_config.TextColumn(width="small"),
        "Webinar":st.column_config.TextColumn(width="large"),
        "Mailbox":st.column_config.TextColumn(width="medium"),
        "Classified At":st.column_config.TextColumn(width="small"),
    }

# Format date
df["Classified At"] = pd.to_datetime(df["Classified At"], errors="coerce").dt.strftime("%Y-%m-%d")

# Search filter (applied after load)
if search_term:
    mask = pd.Series(False, index=df.index)
    for col in df.columns:
        mask |= df[col].astype(str).str.contains(search_term, case=False, na=False)
    df = df[mask]

# ── Metrics ───────────────────────────────────────────────────────────────────
m1, m2, m3 = st.columns(3)
m1.metric("Showing",  f"{len(df):,} records")
m2.metric("Category", collection_choice)
m3.metric("Mailbox",  selected_mailbox)

st.divider()

# ── Breakdown ─────────────────────────────────────────────────────────────────
breakdown_col = "Industry" if collection_choice == "Prospects" else "Type"
with st.expander(f"Breakdown by {breakdown_col.lower()}", expanded=False):
    bd = df[breakdown_col].value_counts().reset_index()
    bd.columns = [breakdown_col, "Count"]
    bc1, bc2 = st.columns([1, 2])
    bc1.dataframe(bd, width='stretch', hide_index=True)
    with bc2:
        st.bar_chart(df[breakdown_col].value_counts())

st.divider()

# ── Table ─────────────────────────────────────────────────────────────────────
st.dataframe(
    df,
    width='stretch',
    hide_index=True,
    height=550,
    column_config=col_config,
)

# ── Download ──────────────────────────────────────────────────────────────────
st.divider()

mb_slug = selected_mailbox.replace("@", "_at_").replace(".", "_")
if collection_choice == "Prospects":
    fname = f"prospects_{status_choice.lower()}_{mb_slug}.csv"
    label = f"⬇ Download Prospects CSV ({len(df):,} rows)"
else:
    fname = f"{collection_choice.lower()}_{mb_slug}.csv"
    label = f"⬇ Download {collection_choice} CSV ({len(df):,} rows)"

csv = df.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    label=label,
    data=csv,
    file_name=fname,
    mime="text/csv",
    width='stretch',
    type="primary",
)
