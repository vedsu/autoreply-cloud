"""Streamlit page for Serper-powered email classification and processing."""
import streamlit as st
import requests
from openai import OpenAI
from core.config import get_secret
from core.mongo import db, COL_PROSPECT, COL_REMOVAL, COL_UNAVAIL, now_utc

st.set_page_config(page_title="Serper Email Classification", page_icon="🔍", layout="wide")

st.title("🔍 Serper + LLM Email Classification")
st.write("Is page par aap emails ya sender domains ko Serper search aur LLM ke madhyam se classify kar sakte hain.")

# Input form for single or batch classification
with st.form("serper_classify_form"):
    sender_email = st.text_input("Sender Email / Domain", placeholder="example@company.com")
    email_subject = st.text_input("Email Subject / Context", placeholder="Webinar Inquiry or OOO")
    email_body = st.text_area("Email Body Preview", placeholder="Paste email content here...")
    
    submitted = st.form_submit_button("Classify via Serper", type="primary")

if submitted:
    if not sender_email:
        st.error("Kripya sender email ya domain enter karein.")
    else:
        with st.spinner("Serper API se web search aur LLM classification ki ja rahi hai..."):
            domain = sender_email.split("@")[-1] if "@" in sender_email else sender_email
            query = f"{domain} company details job title industry"
            
            # 1. Serper Web Search
            snippets = ""
            try:
                serper_resp = requests.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": get_secret("SERPER_API_KEY"), "Content-Type": "application/json"},
                    json={"q": query, "num": 3},
                    timeout=10,
                )
                serper_resp.raise_for_status()
                snippets = "\n".join(
                    f"{item.get('title','')}: {item.get('snippet','')}"
                    for item in serper_resp.json().get("organic", [])[:3]
                )
            except Exception as e:
                st.warning(f"Serper search warning: {e}")

            # 2. LLM Classification using DeepSeek / Gemini
            prompt = f"""
            Analyze the following email and web search snippets to classify the response.
            
            Sender: {sender_email}
            Subject: {email_subject}
            Body: {email_body}
            
            Web Search Context:
            {snippets}
            
            Categories allowed:
            - removal (hard bounce, opt-out, departed, etc.)
            - unavailable (OOO, vacation, leave)
            - prospect (interested lead, valid contact)
            
            Return strictly valid JSON format:
            {{
              "category": "removal | unavailable | prospect",
              "job_title": "...",
              "company": "...",
              "industry": "Healthcare | Pharmaceuticals | Finance | Human Resources | Education | Other",
              "confidence": "high | medium | low"
            }}
            """
            
            try:
                client = OpenAI(api_key=get_secret("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
                response = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                )
                import json
                res_text = response.choices[0].message.content or "{}"
                # Clean markdown blocks if any
                if "```json" in res_text:
                    res_text = res_text.split("```json")[1].split("```")[0].strip()
                elif "```" in res_text:
                    res_text = res_text.split("```")[1].split("```")[0].strip()
                
                result_data = json.loads(res_text)
                
                st.success("Classification Successful!")
                st.json(result_data)
                
                # 3. Store Data to MongoDB based on category
                category = result_data.get("category")
                if category == "prospect":
                    db[COL_PROSPECT].update_one(
                        {"email": sender_email.lower().strip()},
                        {"$set": {
                            "email": sender_email.lower().strip(),
                            "job_title": result_data.get("job_title", ""),
                            "company": result_data.get("company", domain),
                            "industry": result_data.get("industry", "Other"),
                            "webinar": email_subject,
                            "classified_at": now_utc(),
                            "checked": False,
                            "skipped": False
                        }},
                        upsert=True
                    )
                    st.info("Data successfully saved to **Prospects** collection in MongoDB.")
                elif category == "removal":
                    db[COL_REMOVAL].insert_one({
                        "email": sender_email.lower().strip(),
                        "webinar": email_subject,
                        "classified_at": now_utc()
                    })
                    st.info("Data successfully saved to **Removal** collection.")
                elif category == "unavailable":
                    db[COL_UNAVAIL].insert_one({
                        "email": sender_email.lower().strip(),
                        "webinar": email_subject,
                        "classified_at": now_utc()
                    })
                    st.info("Data successfully saved to **Unavailable** collection.")
                    
            except Exception as ex:
                st.error(f"Classification ya Database save karne mein error aaya: {ex}")

st.divider()
st.caption("Serper API + DeepSeek Integrated Streamlit Module")
