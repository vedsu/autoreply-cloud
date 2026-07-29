"""Prospect enrichment via Gemini (Google Search grounding) or Claude (web search tool)."""
import json
import re
from typing import Dict

from core.config import get_secret

_INDUSTRIES = "Healthcare, Pharmaceuticals, Finance, Human Resources, Education, Other"

# Short prompt — keeps input token count low for both providers
_ENRICH_PROMPT = (
    'job title & employer of {name} ({domain}). '
    'JSON only: {{"job_title":"","company":"","industry":"Healthcare|Pharmaceuticals|Finance|Human Resources|Education|Other"}}'
)

# No system message for Claude — saves ~15 input tokens; prompt is self-contained


def _clean(val: str) -> str:
    return val.replace("*", "").replace("_", "").strip() if val else ""


def _extract_json(text: str) -> Dict:
    if not text:
        return {}
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    try:
        return json.loads(text.strip())
    except Exception:
        return {}


def _build_prompt(prospect: Dict) -> str:
    email  = prospect.get("email", "")
    domain = email.split("@")[-1] if "@" in email else email
    name   = prospect.get("prospect_name") or domain
    return _ENRICH_PROMPT.replace("{name}", name).replace("{domain}", domain)


def _normalise(result: Dict) -> Dict:
    valid    = {i.strip() for i in _INDUSTRIES.split(",")}
    industry = _clean(result.get("industry", ""))
    if industry not in valid:
        industry = "Other"
    return {
        "job_title": _clean(result.get("job_title", "")),
        "company":   _clean(result.get("company", "")),
        "industry":  industry,
    }


# ---------------------------------------------------------------------------
# Gemini with Google Search grounding  (uses GEMINI_SEARCH_KEY)
# ---------------------------------------------------------------------------

def enrich_gemini(prospect: Dict) -> Dict:
    from google import genai
    from google.genai import types as gtypes

    # Use the dedicated search key (new AI Studio project)
    client = genai.Client(api_key=get_secret("GEMINI_SEARCH_KEY"))
    prompt = _build_prompt(prospect)

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                tools=[gtypes.Tool(google_search=gtypes.GoogleSearch())],
                temperature=0,
                # Disable extended thinking — saves ~500–1000 tokens per call
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
                # Cap output — our JSON is ~60 chars; 80 tokens is plenty
                max_output_tokens=80,
            ),
        )
        # response.text raises if the response has grounding metadata but no clean text part
        try:
            text = response.text or ""
        except Exception:
            text = ""
            if response.candidates:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "text") and part.text:
                        text += part.text

        result = _extract_json(text)
        if result:
            return _normalise(result)
        return {"error": f"No JSON in Gemini response: {text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Claude with web search tool  (uses CLAUDE_API_KEY)
# ---------------------------------------------------------------------------

def enrich_claude(prospect: Dict) -> Dict:
    import anthropic

    # web_search_20250305 is a server-side tool — Anthropic executes the search
    # and returns server_tool_use + web_search_tool_result + text all in ONE call
    # with stop_reason="end_turn". No multi-turn loop needed.
    client = anthropic.Anthropic(api_key=get_secret("CLAUDE_API_KEY"))
    prompt = _build_prompt(prospect)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            # search result block itself can be 300-500 tokens; keep 600 as ceiling
            max_tokens=600,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
            messages=[{"role": "user", "content": prompt}],
        )

        if response.stop_reason != "end_turn":
            return {"error": f"Unexpected stop: {response.stop_reason}"}

        for block in response.content:
            block_text = getattr(block, "text", None) or ""
            if block_text:
                result = _extract_json(block_text)
                if result:
                    return _normalise(result)

        return {"error": "No JSON in Claude response"}

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# DeepSeek-Chat + Serper web search  (uses SERPER_API_KEY + DEEPSEEK_API_KEY)
# ---------------------------------------------------------------------------

def enrich_deepseek(prospect: Dict) -> Dict:
    import requests
    from openai import OpenAI

    email  = prospect.get("email", "")
    domain = email.split("@")[-1] if "@" in email else email
    name   = prospect.get("prospect_name") or ""
    query  = f"{name} {domain} job title company linkedin".strip() if name else f"{domain} company job title"

    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": get_secret("SERPER_API_KEY"), "Content-Type": "application/json"},
            json={"q": query, "num": 3},
            timeout=10,
        )
        r.raise_for_status()
        snippets = "\n".join(
            f"{item.get('title','')}: {item.get('snippet','')}"
            for item in r.json().get("organic", [])[:3]
        )
    except Exception as e:
        return {"error": f"Serper search failed: {e}"}

    try:
        client = OpenAI(api_key=get_secret("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
        prompt = (
            f"Extract job title, company and industry for {name or domain} ({domain}) from these search results.\n\n"
            f"{snippets}\n\n"
            f'JSON only: {{"job_title":"","company":"","industry":"Healthcare|Pharmaceuticals|Finance|Human Resources|Education|Other"}}'
        )
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0,
        )
        text = resp.choices[0].message.content or ""
        result = _extract_json(text)
        if result:
            return _normalise(result)
        return {"error": f"No JSON in DeepSeek response: {text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def enrich(prospect: Dict, provider: str = "DeepSeek") -> Dict:
    if provider == "Gemini":
        return enrich_gemini(prospect)
    if provider == "Claude":
        return enrich_claude(prospect)
    return enrich_deepseek(prospect)
