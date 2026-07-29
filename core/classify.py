"""Gemini-based batch email classification."""
import json
import re
import time
from typing import Any, Dict, List

from core.config import get_secret


_STRIP_PREFIX = re.compile(
    r"^(?:re|fw|fwd|automatic\s*reply|auto[\s\-]?reply|auto[\s\-]?response|"
    r"out\s*of\s*(?:the\s*)?office|ooo|automatische\s+antwort|abwesenheitsnotiz|"
    r"r[eé]ponse\s+automatique|absence\s+du\s+bureau|respuesta\s+autom[aá]tica|"
    r"resposta\s+autom[aá]tica|automatisk\s+svar|otomatik\s+yan[iı]t|"
    r"自動回覆|自动回复|自動応答|автоматична\s+відповідь|автоответ|"
    r"undeliverable|delivery\s+status\s+notification|afwezigheidsmelding|"
    r"automatisch\s+antwoord|risposta\s+automatica)[:\s\-–]*",
    re.IGNORECASE,
)
_STRIP_TAGS = re.compile(
    r"^\[?\s*(?:ext|external|externo|\*+possible\s+spam\*+)\s*\]?\s*[:\-]?\s*",
    re.IGNORECASE,
)


def strip_subject_prefix(subject: str) -> str:
    s = _STRIP_PREFIX.sub("", subject.strip())
    s = _STRIP_TAGS.sub("", s)
    return s.strip() or subject.strip()


_CLASSIFICATION_PROMPT = """\
You are classifying email responses to a B2B healthcare webinar marketing campaign (US market).

### Categories

**removal** — permanently remove sender from campaign list:
  - Hard bounce (address does not exist)
  - Soft bounce (temporary delivery failure / quota exceeded)
  - Explicit opt-out / unsubscribe / "remove me" request
  - Inactive / invalid address (auto-reply says this inbox is no longer active)
  - Person departed, retired, resigned, or deceased

**unavailable** — temporarily away; keep on campaign list:
  - Out of office / OOO
  - Vacation / holiday
  - Parental, medical, or sick leave

**prospect** — a new contact to pursue:
  - Direct human reply showing interest, asking a question, providing contact info, or engaging
  - Auto-reply that mentions an alternative contact person (even if the primary category is "unavailable")

### Output format
Return a **JSON array** — one object per input email — with this exact shape:

```
{
  "message_id": "...",
  "category": "removal | unavailable | prospect",
  "removal_type": "bounce_hard | bounce_soft | opt_out | inactive | departed | deceased",  // only if removal
  "unavailable_type": "out_of_office | vacation | on_leave | medical_leave",               // only if unavailable
  "source_type": "direct_reply",                                                            // only if prospect
  "job_title": "...",    // extracted from body/signature if visible; else null
  "redirect": {          // fill if the body mentions an alternative contact; else null
    "email": "...",
    "name":  "...",
    "job_title": "...",
    "phone": "..."
  },
  "webinar": "...",      // original campaign subject (strip OOO/bounce prefix AND [EXTERNAL]/[EXT] tags)
  "confidence": "high | medium | low"
}
```

Important:
- For emails that are **unavailable** OR **removal** but also contain a redirect contact, set the primary category
  for the sender correctly AND populate `redirect` with the alternative contact.
- For direct prospect replies (category = "prospect"), set `source_type: "direct_reply"` and leave `redirect` null.
- Always strip OOO/reply prefixes AND [EXTERNAL], [EXT], [*SPAM*] tags from the webinar subject.
- Return ONLY valid JSON — no markdown, no explanation.

### Emails to classify
{email_json}
"""


def _build_gemini_client():
    from google import genai
    return genai.Client(api_key=get_secret("GEMINI_API_KEY"))


def _parse_gemini_response(text: str) -> List[Dict]:
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except Exception:
        pass
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return []


def _call_gemini(prompt: str) -> List[Dict]:
    from google import genai
    from google.genai import types as gtypes

    client = _build_gemini_client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return _parse_gemini_response(response.text or "")


def _call_deepseek(prompt: str) -> List[Dict]:
    from openai import OpenAI
    client = OpenAI(api_key=get_secret("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3000,
        temperature=0.1,
    )
    return _parse_gemini_response(resp.choices[0].message.content or "")


def classify_batch(docs: List[Dict[str, Any]], max_retries: int = 3) -> List[Dict[str, Any]]:
    """
    Classify a batch of raw email documents via Gemini 2.5 Flash.
    Retries with exponential backoff on transient failures.
    Splits into halves whenever Gemini returns fewer results than expected
    (partial omissions or full failure), recursing down to single emails.
    """
    if not docs:
        return []

    payload = [
        {
            "message_id":    d.get("message_id", ""),
            "from_email":    d.get("from_email", ""),
            "from_name":     d.get("from_name", ""),
            "subject":       d.get("subject", ""),
            "body_preview":  (d.get("body") or "")[:700],
            "emails_found":  (d.get("emails_found") or [])[:10],
        }
        for d in docs
    ]
    prompt = _CLASSIFICATION_PROMPT.replace("{email_json}", json.dumps(payload, ensure_ascii=False))

    last_exc = None
    best: List[Dict] = []

    for attempt in range(1, max_retries + 1):
        try:
            results = _call_gemini(prompt)
            if len(results) == len(docs):
                return results          # perfect — all emails classified
            if len(results) > len(best):
                best = results          # keep best partial result so far
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        except Exception as exc:
            last_exc = exc
            delay = 2 ** attempt
            m = re.search(r"'retryDelay':\s*'(\d+)s'", str(exc))
            if m:
                delay = int(m.group(1)) + 2
            if attempt < max_retries:
                time.sleep(delay)

    # Gemini exhausted — try DeepSeek as fallback before splitting
    try:
        ds_results = _call_deepseek(prompt)
        if len(ds_results) == len(docs):
            return ds_results
        if len(ds_results) > len(best):
            best = ds_results
    except Exception:
        pass

    # Still incomplete — split and recurse
    if len(docs) > 1:
        mid   = len(docs) // 2
        left  = classify_batch(docs[:mid], max_retries=2)
        right = classify_batch(docs[mid:], max_retries=2)
        return left + right

    # Single-email leaf: return whatever we got (may be empty)
    if last_exc and not best:
        raise last_exc
    return best


def _domain(email: str) -> str:
    """Return lowercase domain part of an email address."""
    return email.lower().split("@")[-1] if "@" in email else ""


def process_classification_results(
    results: List[Dict],
    docs: List[Dict],
    source_mailbox: str,
) -> Dict[str, int]:
    """
    Write classification results to MongoDB.
    Returns count dict: {removal, unavailable, prospect, redirects, errors}.
    """
    from core.mongo import insert_removal, insert_unavailable, insert_prospect

    counts = {"removal": 0, "unavailable": 0, "prospect": 0, "redirects": 0, "errors": 0}
    sending_domain = _domain(source_mailbox)

    # Build lookup by message_id for the original docs
    doc_by_mid = {d.get("message_id", ""): d for d in docs}

    for res in results:
        try:
            mid      = res.get("message_id", "")
            doc      = doc_by_mid.get(mid, {})
            category = res.get("category", "")
            webinar  = res.get("webinar") or strip_subject_prefix(doc.get("subject", ""))
            sender   = doc.get("from_email", res.get("from_email", ""))
            name     = doc.get("from_name",  res.get("from_name",  ""))

            if category == "removal":
                insert_removal({
                    "email":          sender,
                    "name":           name,
                    "removal_type":   res.get("removal_type", ""),
                    "webinar":        webinar,
                    "source_mailbox": source_mailbox,
                })
                counts["removal"] += 1

            elif category == "unavailable":
                insert_unavailable({
                    "email":              sender,
                    "name":               name,
                    "unavailable_type":   res.get("unavailable_type", ""),
                    "webinar":            webinar,
                    "source_mailbox":     source_mailbox,
                })
                counts["unavailable"] += 1

            elif category == "prospect":
                # Skip if the prospect's domain is our own sending domain
                if _domain(sender) != sending_domain:
                    insert_prospect({
                        "email":                 sender,
                        "prospect_name":         name,
                        "job_title":             res.get("job_title") or "",
                        "company":               (sender.split("@")[-1] if "@" in sender else ""),
                        "industry":              "",
                        "webinar":               webinar,
                        "source_type":           res.get("source_type", "direct_reply"),
                        "original_sender_email": "",
                        "source_mailbox":        source_mailbox,
                    })
                    counts["prospect"] += 1

            # Handle redirect prospect (present alongside removal or unavailable)
            redirect = res.get("redirect")
            if redirect and isinstance(redirect, dict) and redirect.get("email"):
                r_email = redirect["email"].lower().strip()
                # Skip if same as sender, our own account, or matches our sending domain
                if r_email and r_email != sender.lower() and _domain(r_email) != sending_domain:
                    insert_prospect({
                        "email":                 r_email,
                        "prospect_name":         redirect.get("name", ""),
                        "job_title":             redirect.get("job_title", ""),
                        "company":               (r_email.split("@")[-1] if "@" in r_email else ""),
                        "industry":              "",
                        "webinar":               webinar,
                        "source_type":           f"redirect_from_{category}",
                        "original_sender_email": sender,
                        "source_mailbox":        source_mailbox,
                        "phone":                 redirect.get("phone", ""),
                    })
                    counts["redirects"] += 1

        except Exception as e:
            counts["errors"] += 1

    return counts
