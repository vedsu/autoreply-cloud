"""IMAP sync logic — ported from auto_reply_app.py, UI-free."""
import email
import imaplib
import re
import socket
from datetime import date, timedelta
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------

def safe_decode_bytes(data, encoding=None) -> str:
    if data is None:
        return ""
    candidates = []
    if encoding:
        enc = str(encoding).lower().strip()
        candidates.append("cp874" if enc == "windows-874" else enc)
    candidates += ["utf-8", "cp1252", "latin-1", "iso-8859-1"]
    for enc in candidates:
        try:
            return data.decode(enc, errors="ignore")
        except Exception:
            pass
    return data.decode("utf-8", errors="ignore")


def decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        parts = decode_header(value)
    except Exception:
        return str(value)
    out = ""
    for part, enc in parts:
        out += safe_decode_bytes(part, enc) if isinstance(part, bytes) else str(part)
    return out.strip()


def parse_address(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {"name": "", "email": "", "raw": ""}
    name, addr = parseaddr(raw)
    return {
        "name": decode_mime_header(name),
        "email": str(addr or "").lower().strip(),
        "raw": decode_mime_header(raw),
    }


def parse_email_date(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {"raw": "", "iso": "", "date": "", "datetime": ""}
    try:
        dt = parsedate_to_datetime(raw)
        return {
            "raw": raw,
            "iso": dt.isoformat(),
            "date": dt.date().isoformat(),
            "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception:
        return {"raw": raw, "iso": "", "date": "", "datetime": ""}


def strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?s)<.*?>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_body(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_emails(text: Optional[str]) -> List[str]:
    if not text:
        return []
    found = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    seen, unique = set(), []
    for e in found:
        addr = e.lower().strip()
        if addr not in seen:
            seen.add(addr)
            unique.append(addr)
    return unique


# ---------------------------------------------------------------------------
# IMAP connection
# ---------------------------------------------------------------------------

def connect_and_login(email_id: str, password: str, host: str, port: int, timeout: int = 60):
    socket.setdefaulttimeout(timeout)
    mail = imaplib.IMAP4_SSL(host, port) if port in (993, 995) else imaplib.IMAP4(host, port)
    mail.login(email_id, password)
    return mail


def list_mailboxes(mail) -> List[str]:
    status, boxes = mail.list()
    if status != "OK" or not boxes:
        return []
    names = []
    for box in boxes:
        decoded = box.decode(errors="ignore") if isinstance(box, bytes) else box
        # Format: (flags) "separator" folder_name
        # folder_name may be quoted or unquoted — split after the separator token
        m = re.search(r'"[^"]*"\s+"?([^"]+)"?\s*$', decoded)
        if m:
            names.append(m.group(1).strip())
        else:
            names.append(decoded.split()[-1].strip('"'))
    return sorted(set(names))


def select_folder(mail, folder: str) -> bool:
    for name in [f'"{folder}"', folder]:
        try:
            status, _ = mail.select(name, readonly=True)
            if status == "OK":
                return True
        except Exception:
            pass
    return False


def find_spam_folder(mail) -> Optional[str]:
    folders = list_mailboxes(mail)
    candidates = ["Spam", "Junk", "Junk E-mail", "[Gmail]/Spam", "INBOX.Spam", "Bulk Mail"]
    lower_map = {f.lower(): f for f in folders}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    for f in folders:
        if "spam" in f.lower() or "junk" in f.lower():
            return f
    return None


def find_trash_folder(mail) -> Optional[str]:
    folders = list_mailboxes(mail)
    candidates = ["INBOX.Trash", "Trash", "[Gmail]/Trash", "INBOX.trash", "Deleted Items", "Deleted Messages"]
    lower_map = {f.lower(): f for f in folders}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    for f in folders:
        if "trash" in f.lower() or "deleted" in f.lower():
            return f
    return None


def _delete_folder_messages(
    mail, folder: str, message_ids: set, trash_folder: Optional[str]
) -> Tuple[int, int, int]:
    """Select folder (writable), find messages by Message-ID, move to Trash / expunge."""
    deleted = not_found = errors = 0
    for name in [f'"{folder}"', folder]:
        try:
            status, _ = mail.select(name)
            if status == "OK":
                break
        except Exception:
            pass
    else:
        return deleted, not_found, errors

    seqs_to_delete: List[bytes] = []
    for mid in message_ids:
        try:
            mid_clean = mid.strip("<>").strip()
            status, data = mail.search(None, f'HEADER Message-ID "<{mid_clean}>"')
            if status != "OK" or not data or not data[0].strip():
                status, data = mail.search(None, f'HEADER Message-ID "{mid_clean}"')
            if status == "OK" and data and data[0].strip():
                for seq in data[0].split():
                    seqs_to_delete.append(seq)
                deleted += 1
            else:
                not_found += 1
        except Exception:
            errors += 1

    if seqs_to_delete:
        seq_str = ",".join(s.decode() if isinstance(s, bytes) else s for s in seqs_to_delete)
        try:
            if trash_folder:
                mail.copy(seq_str, f'"{trash_folder}"')
            mail.store(seq_str, "+FLAGS", "\\Deleted")
            mail.expunge()
        except Exception:
            pass

    return deleted, not_found, errors


def delete_inbox_emails(
    email_id: str, password: str, host: str, port: int,
    message_ids: List[str],
    on_status: Optional[Callable] = None,
    timeout: int = 60,
) -> Dict:
    """Move classified emails from INBOX (+spam) to Trash via IMAP."""
    if not message_ids:
        return {"deleted": 0, "not_found": 0, "errors": 0, "error": None}

    mid_set = set(message_ids)
    counts = {"deleted": 0, "not_found": 0, "errors": 0, "error": None}

    try:
        mail = connect_and_login(email_id, password, host, port, timeout)
    except Exception as e:
        counts["error"] = f"IMAP login failed: {e}"
        return counts

    try:
        trash = find_trash_folder(mail)
        if on_status:
            on_status(f"Moving {len(mid_set)} emails to trash ({trash or 'expunge only'})…")

        d, n, e = _delete_folder_messages(mail, "INBOX", mid_set, trash)
        counts["deleted"]   += d
        counts["not_found"] += n
        counts["errors"]    += e

        spam = find_spam_folder(mail)
        if spam:
            d, n, e = _delete_folder_messages(mail, spam, mid_set, trash)
            counts["deleted"]   += d
            counts["not_found"] += n
            counts["errors"]    += e

    except Exception as e:
        counts["error"] = str(e)
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return counts


def folder_count(mail, folder: str) -> int:
    if not select_folder(mail, folder):
        return 0
    status, ids = mail.search(None, "ALL")
    return len(ids[0].split()) if status == "OK" and ids else 0


def earliest_date(mail, folder: str) -> Dict[str, str]:
    if not select_folder(mail, folder):
        return {"date": "", "message": "Folder not found"}
    status, ids = mail.search(None, "ALL")
    if status != "OK" or not ids or not ids[0]:
        return {"date": "", "message": "No emails"}
    msg_list = ids[0].split()
    sample = msg_list[:10] + (msg_list[-10:] if len(msg_list) > 10 else [])
    parsed = []
    for seq in sample:
        try:
            h = fetch_header(mail, seq)
            if h.get("date_raw"):
                dt = parsedate_to_datetime(h["date_raw"])
                parsed.append({"dt": dt, "date": dt.date().isoformat()})
        except Exception:
            pass
    if not parsed:
        return {"date": "", "message": "Could not parse"}
    return {"date": min(parsed, key=lambda x: x["dt"])["date"], "message": ""}


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_header(mail, seq_id) -> Dict[str, str]:
    fields = "DATE FROM TO CC SUBJECT MESSAGE-ID REPLY-TO"
    status, data = mail.fetch(seq_id, f"(BODY.PEEK[HEADER.FIELDS ({fields})])")
    if status != "OK" or not data or not data[0]:
        return {}
    try:
        msg = email.message_from_bytes(data[0][1])
    except Exception:
        return {}
    return {
        "subject":       decode_mime_header(msg.get("Subject")),
        "from_raw":      decode_mime_header(msg.get("From")),
        "to_raw":        decode_mime_header(msg.get("To")),
        "cc_raw":        decode_mime_header(msg.get("Cc")),
        "reply_to_raw":  decode_mime_header(msg.get("Reply-To")),
        "message_id":    str(msg.get("Message-ID") or "").strip(),
        "date_raw":      msg.get("Date") or "",
    }


def fetch_body(mail, seq_id, limit: int) -> str:
    if limit <= 0:
        return ""
    status, data = mail.fetch(seq_id, f"(BODY.PEEK[TEXT]<0.{limit}>)")
    if status != "OK" or not data or not data[0]:
        return ""
    payload = data[0][1]
    if not payload:
        return ""
    text = safe_decode_bytes(payload, "utf-8")
    if any(tag in text.lower() for tag in ("<html", "<body", "<br")):
        text = strip_html(text)
    return clean_body(text)


def build_date_search(start: date, end: date) -> List[str]:
    return ["SINCE", start.strftime("%d-%b-%Y"), "BEFORE", (end + timedelta(days=1)).strftime("%d-%b-%Y")]


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

def test_connection(email_id: str, password: str, host: str, port: int, timeout: int = 30) -> Dict:
    mail = connect_and_login(email_id, password, host, port, timeout)
    try:
        folders    = list_mailboxes(mail)
        spam       = find_spam_folder(mail)
        inbox_cnt  = folder_count(mail, "INBOX")
        spam_cnt   = folder_count(mail, spam) if spam else 0
        return {
            "status":       "success",
            "folders":      folders,
            "spam_folder":  spam,
            "inbox_total":  inbox_cnt,
            "spam_total":   spam_cnt,
            "inbox_earliest": earliest_date(mail, "INBOX"),
        }
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Classify connection errors
# ---------------------------------------------------------------------------

def classify_error(e: Exception) -> str:
    msg = str(e)
    if isinstance(e, socket.gaierror):
        return f"DNS lookup failed — server not found. Check hostname.\n{msg}"
    if isinstance(e, socket.timeout):
        return f"Connection timed out. Try a different port.\n{msg}"
    if isinstance(e, (ConnectionRefusedError,)) or "10061" in msg:
        return f"Connection refused. Check host/port.\n{msg}"
    if isinstance(e, imaplib.IMAP4.error):
        if any(w in msg.lower() for w in ("login", "auth", "credentials", "invalid")):
            return f"Authentication failed. Check email and password.\n{msg}"
        return f"IMAP error: {msg}"
    return f"Connection failed: {msg}"


# ---------------------------------------------------------------------------
# Full fetch pipeline
# ---------------------------------------------------------------------------

def fetch_folder(
    email_id: str, password: str, host: str, port: int, timeout: int,
    folder: str, start: date, end: date,
    max_emails: int, body_limit: int, reconnect_every: int,
    on_progress: Optional[Callable] = None,
    on_status: Optional[Callable] = None,
    progress_base: float = 0.0,
    progress_span: float = 1.0,
) -> Tuple[List[Dict], List[Dict]]:
    records, skipped = [], []
    criteria = build_date_search(start, end)
    mail = None
    try:
        mail = connect_and_login(email_id, password, host, port, timeout)
        if not select_folder(mail, folder):
            return records, skipped
        status, ids = mail.search(None, *criteria)
        if status != "OK" or not ids:
            return records, skipped
        msg_list = ids[0].split()
        if max_emails > 0:
            msg_list = msg_list[-max_emails:]
        total = len(msg_list)
        if total == 0:
            return records, skipped

        for idx, seq_id in enumerate(msg_list, 1):
            seq_text = seq_id.decode(errors="ignore") if isinstance(seq_id, bytes) else str(seq_id)
            try:
                if reconnect_every > 0 and idx > 1 and (idx - 1) % reconnect_every == 0:
                    try:
                        mail.logout()
                    except Exception:
                        pass
                    mail = connect_and_login(email_id, password, host, port, timeout)
                    select_folder(mail, folder)
                    if on_status:
                        on_status(f"Reconnected at {idx}/{total}")

                hdr = fetch_header(mail, seq_id)
                if not hdr:
                    skipped.append({"folder": folder, "seq": seq_text, "error": "Header fetch failed"})
                    continue

                body = ""
                try:
                    body = fetch_body(mail, seq_id, body_limit)
                except Exception as be:
                    skipped.append({"folder": folder, "seq": seq_text, "error": f"Body skipped: {be}"})

                from_p      = parse_address(hdr.get("from_raw", ""))
                to_p        = parse_address(hdr.get("to_raw", ""))
                reply_to_p  = parse_address(hdr.get("reply_to_raw", ""))
                date_info   = parse_email_date(hdr.get("date_raw", ""))

                all_emails = []
                for val in [hdr.get("from_raw",""), hdr.get("to_raw",""), hdr.get("cc_raw",""), hdr.get("reply_to_raw",""), body]:
                    all_emails.extend(extract_emails(val))
                all_emails = list(dict.fromkeys(all_emails))

                records.append({
                    "folder":          folder,
                    "sequence_id":     seq_text,
                    "message_id":      hdr.get("message_id", ""),
                    "subject":         hdr.get("subject", ""),
                    "from_raw":        hdr.get("from_raw", ""),
                    "from_name":       from_p["name"],
                    "from_email":      from_p["email"],
                    "to_raw":          hdr.get("to_raw", ""),
                    "to_email":        to_p["email"],
                    "cc_raw":          hdr.get("cc_raw", ""),
                    "reply_to_raw":    hdr.get("reply_to_raw", ""),
                    "reply_to_email":  reply_to_p["email"],
                    "received_at_raw": date_info["raw"],
                    "received_at_iso": date_info["iso"],
                    "received_date":   date_info["date"],
                    "body":            body,
                    "body_preview":    body[:500],
                    "emails_found":    all_emails,
                })
            except Exception as e:
                skipped.append({"folder": folder, "seq": seq_text, "error": str(e)})
                try:
                    mail.logout()
                except Exception:
                    pass
                try:
                    mail = connect_and_login(email_id, password, host, port, timeout)
                    select_folder(mail, folder)
                except Exception:
                    pass

            if on_progress:
                pct = progress_base + (idx / total) * progress_span
                on_progress(min(pct, 1.0))
            if on_status:
                on_status(f"{folder}: {idx}/{total}")

    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass
    return records, skipped


def fetch_all(
    email_id: str, password: str, host: str, port: int, timeout: int,
    start: date, end: date, max_emails: int, body_limit: int, reconnect_every: int,
    on_progress: Optional[Callable] = None,
    on_status: Optional[Callable] = None,
) -> Dict:
    info = test_connection(email_id, password, host, port, timeout)
    spam = info.get("spam_folder")

    inbox_docs, inbox_skip = fetch_folder(
        email_id, password, host, port, timeout,
        "INBOX", start, end, max_emails, body_limit, reconnect_every,
        on_progress, on_status, 0.0, 0.5 if spam else 1.0,
    )
    spam_docs, spam_skip = [], []
    if spam:
        spam_docs, spam_skip = fetch_folder(
            email_id, password, host, port, timeout,
            spam, start, end, max_emails, body_limit, reconnect_every,
            on_progress, on_status, 0.5, 0.5,
        )

    return {
        "sync_metadata": {
            "email":          email_id,
            "host":           host,
            "port":           port,
            "date_range":     f"{start.isoformat()} to {end.isoformat()}",
            "folders":        ["INBOX"] + ([spam] if spam else []),
            "spam_folder":    spam,
            "inbox_total":    info.get("inbox_total", 0),
            "spam_total":     info.get("spam_total", 0),
            "inbox_fetched":  len(inbox_docs),
            "spam_fetched":   len(spam_docs),
            "total_fetched":  len(inbox_docs) + len(spam_docs),
            "skipped":        len(inbox_skip) + len(spam_skip),
            "body_bytes_limit": body_limit,
        },
        "emails":          inbox_docs + spam_docs,
        "skipped":         inbox_skip + spam_skip,
    }
