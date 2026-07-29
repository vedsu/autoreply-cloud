"""Page 4 — Prospect review queue (st.fragment, one card at a time)."""
import streamlit as st

from core.mongo import (
    get_accounts, get_all_statuses,
    count_prospects, get_next_prospect, mark_checked, mark_skipped, mark_generic,
    upsert_status, get_all_prospects, update_prospect_fields, search_prospects,
    COL_PROSPECT, db,
)
from core.enrich import enrich

_INDUSTRIES   = ["Healthcare", "Pharmaceuticals", "Finance", "Human Resources", "Education", "Other"]
_IND_WITH_ALL = ["All"] + _INDUSTRIES

st.set_page_config(page_title="Review", page_icon="🔍", layout="wide")
st.title("🔍 Review Prospects")

# ── Sidebar — metrics + browse/edit skipped ───────────────────────────────────
with st.sidebar:
    st.header("📋 Prospect Data")

    total_processed = db()[COL_PROSPECT].count_documents({"checked": True, "skipped": False})
    total_skipped   = db()[COL_PROSPECT].count_documents({"skipped": True, "generic": {"$ne": True}})
    total_generic   = db()[COL_PROSPECT].count_documents({"generic": True})

    c1, c2, c3 = st.columns(3)
    c1.metric("✅ Done",    total_processed)
    c2.metric("⏭ Skipped", total_skipped)
    c3.metric("🏷 Generic", total_generic)

    st.divider()
    st.subheader("Browse & Edit")

    view = st.radio("Show", ["Skipped", "Processed"], horizontal=True)
    if view == "Skipped":
        prospects_list = [p for p in get_all_prospects(skipped=True, limit=300)
                          if not p.get("generic")]
    else:
        prospects_list = get_all_prospects(checked=True, skipped=False, limit=300)

    if not prospects_list:
        st.info(f"No {view.lower()} prospects.")
    else:
        sel_email = st.selectbox("Select prospect",
                                 [p["email"] for p in prospects_list], key="sidebar_sel")
        sel_p = next((p for p in prospects_list if p["email"] == sel_email), None)

        if sel_p:
            st.caption(f"Name: {sel_p.get('prospect_name') or '—'}")
            with st.form("edit_prospect_form", border=True):
                new_job   = st.text_input("Job Title", value=sel_p.get("job_title", "") or "")
                cur_ind   = sel_p.get("industry", "Other") or "Other"
                ind_idx   = _INDUSTRIES.index(cur_ind) if cur_ind in _INDUSTRIES else len(_INDUSTRIES) - 1
                new_ind   = st.selectbox("Industry", _INDUSTRIES, index=ind_idx)
                new_co    = st.text_input("Company",  value=sel_p.get("company", "") or "")
                upd_email = st.checkbox("Also update email", value=False)
                new_email = st.text_input("Email", value=sel_p.get("email", ""),
                                          disabled=not upd_email)
                sv, sk, sg = st.columns(3)
                with sv:
                    if st.form_submit_button("💾 Save", width='stretch', type="primary"):
                        fields = {"job_title": new_job.strip(), "industry": new_ind,
                                  "company": new_co.strip(), "checked": True, "skipped": False}
                        if upd_email:
                            fields["email"] = new_email.strip()
                        update_prospect_fields(str(sel_p["_id"]), fields)
                        st.rerun()
                with sk:
                    if st.form_submit_button("⏭ Skip", width='stretch'):
                        mark_skipped(str(sel_p["_id"]))
                        st.rerun()
                with sg:
                    if st.form_submit_button("🏷 Generic", width='stretch'):
                        mark_generic(str(sel_p["_id"]))
                        st.rerun()

# ── Main area — mailbox selector ──────────────────────────────────────────────
accounts = get_accounts()
statuses = {s["email"]: s for s in get_all_statuses()}

if not accounts:
    st.warning("No accounts. Go to **1 Accounts** first.")
    st.stop()

eligible = [
    a for a in accounts
    if statuses.get(a["email"], {}).get("status") in ("classified", "reviewing", "archived")
]

if not eligible:
    st.info("No classified mailboxes yet. Run **3 Classify** first.")
    st.stop()

selected_email = st.selectbox("Select Mailbox", [a["email"] for a in eligible])

if statuses.get(selected_email, {}).get("status") == "classified":
    upsert_status(selected_email, {"status": "reviewing"})

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_review, tab_search = st.tabs(["📬 Review Queue", "🔍 Search & Edit"])

# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — Review Queue
# ─────────────────────────────────────────────────────────────────────────────
with tab_review:
    s               = statuses.get(selected_email, {})
    total_prospects = (s.get("classified_counts", {}).get("prospect", 0) +
                       s.get("classified_counts", {}).get("redirects", 0))
    remaining       = count_prospects(selected_email, unchecked_only=True)
    reviewed        = max(0, total_prospects - remaining)

    st.progress(
        reviewed / total_prospects if total_prospects else 1.0,
        text=f"**{reviewed}** reviewed · **{remaining}** remaining",
    )
    st.divider()

    @st.fragment
    def review_card():
        prospect = get_next_prospect(selected_email)

        if not prospect:
            st.success("✅ All prospects for this mailbox have been reviewed!")
            upsert_status(selected_email, {"status": "classified"})
            return

        doc_id = str(prospect["_id"])

        with st.container(border=True):
            st.markdown(f"### {prospect.get('email', '')}")
            col_l, col_r = st.columns([3, 2])
            with col_l:
                st.write(f"**Name:**      {prospect.get('prospect_name') or '—'}")
                st.write(f"**Job Title:** {prospect.get('job_title') or '—'}")
                st.write(f"**Company:**   {prospect.get('company') or '—'}")
                st.write(f"**Industry:**  {prospect.get('industry') or '—'}")
                st.write(f"**Webinar:**   {prospect.get('webinar') or '—'}")
            with col_r:
                st.write(f"**Source:**    {prospect.get('source_type','—').replace('_',' ')}")
                if prospect.get("original_sender_email"):
                    st.write(f"**Via:**       {prospect['original_sender_email']}")
                if prospect.get("phone"):
                    st.write(f"**Phone:**     {prospect['phone']}")
                mbxs = prospect.get("source_mailboxes") or [prospect.get("source_mailbox", "")]
                st.caption(f"Mailbox: {', '.join(m for m in mbxs if m)}")

        suggestion_key = f"suggestion_{doc_id}"
        suggestion_for = st.session_state.get("suggestion_for")

        if suggestion_for == doc_id and suggestion_key in st.session_state:
            suggestion = st.session_state[suggestion_key]

            if "error" in suggestion:
                st.error(f"Enrichment failed: {suggestion['error']}")
            else:
                job = suggestion.get("job_title") or "—"
                co  = suggestion.get("company")   or "—"
                ind = suggestion.get("industry")  or "—"
                st.markdown(
                    f"""<div style="background:linear-gradient(135deg,#1e3a5f 0%,#0d2137 100%);
                        border-radius:12px;padding:20px 24px;margin:10px 0 14px 0;
                        border-left:4px solid #4a9eff;">
                      <p style="margin:0 0 14px 0;color:#90bde8;font-size:11px;
                                 letter-spacing:1.5px;text-transform:uppercase;font-weight:600;">
                        Enrichment Suggestion</p>
                      <div style="display:flex;gap:32px;flex-wrap:wrap;">
                        <div><p style="margin:0;color:#90bde8;font-size:11px;text-transform:uppercase;">Job Title</p>
                          <p style="margin:4px 0 0 0;color:#fff;font-size:17px;font-weight:700;">{job}</p></div>
                        <div><p style="margin:0;color:#90bde8;font-size:11px;text-transform:uppercase;">Company</p>
                          <p style="margin:4px 0 0 0;color:#fff;font-size:17px;font-weight:700;">{co}</p></div>
                        <div><p style="margin:0;color:#90bde8;font-size:11px;text-transform:uppercase;">Industry</p>
                          <p style="margin:4px 0 0 0;color:#4a9eff;font-size:15px;font-weight:600;">{ind}</p></div>
                      </div></div>""",
                    unsafe_allow_html=True,
                )

            b1, b2 = st.columns(2)
            with b1:
                if st.button("✅ OK — Apply & Next", type="primary", width='stretch'):
                    update = {}
                    if "error" not in suggestion:
                        update = {
                            "job_title": suggestion.get("job_title") or prospect.get("job_title", ""),
                            "company":   suggestion.get("company")   or prospect.get("company", ""),
                            "industry":  suggestion.get("industry")  or "",
                        }
                    mark_checked(doc_id, update)
                    st.session_state.pop(suggestion_key, None)
                    st.session_state.pop("suggestion_for", None)
                    st.rerun(scope="fragment")
            with b2:
                if st.button("❌ Cancel", width='stretch'):
                    st.session_state.pop(suggestion_key, None)
                    st.session_state.pop("suggestion_for", None)
                    st.rerun(scope="fragment")

        else:
            if prospect.get("job_title"):
                st.caption(f"Current job title: **{prospect['job_title']}**")

            bc1, bc2, bc3, bc4 = st.columns(4)
            with bc1:
                provider = st.selectbox("Enrich via", ["DeepSeek", "Claude"],
                                        key=f"provider_{doc_id}", label_visibility="collapsed")
                if st.button("🔍 Enrich", type="primary", width='stretch'):
                    with st.spinner(f"Searching via {provider}…"):
                        result = enrich(prospect, provider)
                    st.session_state[suggestion_key]   = result
                    st.session_state["suggestion_for"] = doc_id
                    st.rerun(scope="fragment")
            with bc2:
                st.write("")
                if st.button("✅ Confirm", width='stretch', help="Accept as-is"):
                    mark_checked(doc_id, {})
                    st.rerun(scope="fragment")
            with bc3:
                st.write("")
                if st.button("⏭ Skip", width='stretch', help="Review later"):
                    mark_skipped(doc_id)
                    st.rerun(scope="fragment")
            with bc4:
                st.write("")
                if st.button("🏷 Generic", width='stretch',
                             help="Generic/irrelevant — excluded from exports"):
                    mark_generic(doc_id)
                    st.rerun(scope="fragment")

    review_card()

# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — Search & Edit
# ─────────────────────────────────────────────────────────────────────────────
with tab_search:
    st.subheader("Search Prospects")

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        f_job = st.text_input("Job Title keyword", placeholder="e.g. Director, Manager")
    with fc2:
        f_ind = st.selectbox("Industry", _IND_WITH_ALL, key="search_ind")
    with fc3:
        f_email = st.text_input("Email contains", placeholder="e.g. @hospital.com")

    if st.button("🔍 Search", type="primary", width='stretch'):
        st.session_state["search_results"] = search_prospects(
            job_title=f_job.strip(),
            industry="" if f_ind == "All" else f_ind,
            email=f_email.strip(),
        )

    results = st.session_state.get("search_results")
    if results is not None:
        st.caption(f"**{len(results)}** result(s)")
        st.divider()

        if not results:
            st.info("No prospects match those filters.")
        else:
            import pandas as pd
            preview_df = pd.DataFrame([{
                "Email":     r["email"],
                "Name":      r.get("prospect_name") or "—",
                "Job Title": r.get("job_title") or "—",
                "Company":   r.get("company") or "—",
                "Industry":  r.get("industry") or "—",
                "Status":    ("🏷 Generic" if r.get("generic")
                              else "⏭ Skipped" if r.get("skipped")
                              else "✅ Done"),
            } for r in results])

            st.dataframe(preview_df, width='stretch', hide_index=True, height=280)
            st.divider()

            # ── Inline editor ────────────────────────────────────────────────
            st.subheader("Edit selected prospect")
            r_email = st.selectbox("Select to edit",
                                   [r["email"] for r in results], key="search_sel")
            r_p = next((r for r in results if r["email"] == r_email), None)

            if r_p:
                cur_i = r_p.get("industry", "Other") or "Other"
                with st.form("search_edit_form", border=True):
                    e1, e2 = st.columns(2)
                    with e1:
                        s_job = st.text_input("Job Title", value=r_p.get("job_title", "") or "")
                        s_co  = st.text_input("Company",   value=r_p.get("company", "") or "")
                    with e2:
                        s_ind = st.selectbox(
                            "Industry", _INDUSTRIES,
                            index=_INDUSTRIES.index(cur_i) if cur_i in _INDUSTRIES else len(_INDUSTRIES) - 1,
                        )
                        s_status = st.radio("Mark as", ["Confirmed", "Skipped", "Generic"],
                                            horizontal=True,
                                            index=(2 if r_p.get("generic")
                                                   else 1 if r_p.get("skipped") else 0))

                    if st.form_submit_button("💾 Save Changes", type="primary", width='stretch'):
                        fields = {
                            "job_title": s_job.strip(),
                            "company":   s_co.strip(),
                            "industry":  s_ind,
                        }
                        if s_status == "Confirmed":
                            fields.update({"checked": True, "skipped": False, "generic": False})
                        elif s_status == "Skipped":
                            fields.update({"skipped": True, "generic": False})
                        else:
                            fields.update({"generic": True, "checked": True, "skipped": True})
                        update_prospect_fields(str(r_p["_id"]), fields)
                        st.session_state.pop("search_results", None)
                        st.success("Saved ✓")
                        st.rerun()
