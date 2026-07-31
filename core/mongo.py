"""MongoDB connection and all collection/document helpers."""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import streamlit as st
from pymongo import MongoClient, ASCENDING
from bson import ObjectId

from core.config import get_secret

DB_NAME      = "email_autoreply"
COL_ACCOUNTS = "accounts"
COL_STATUS   = "mailbox_processing_status"
COL_REMOVAL  = "removal"
COL_UNAVAIL  = "unavailable"
COL_PROSPECT = "prospect"


@st.cache_resource
def _client() -> MongoClient:
    return MongoClient(get_secret("MONGO_URI"), serverSelectionTimeoutMS=10_000)


def db():
    return _client()[DB_NAME]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Collection name helpers
# ---------------------------------------------------------------------------

def sanitize(email: str) -> str:
    return email.replace("@", "_at_").replace(".", "_dot_")


def staging_col_name(email: str) -> str:
    return sanitize(email)


def sync_meta_col_name(email: str) -> str:
    return f"{sanitize(email)}__sync_meta"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def get_accounts() -> List[Dict]:
    return list(db()[COL_ACCOUNTS].find({}, {"_id": 0}).sort("email", ASCENDING))


def upsert_account(email: str, password: str, server: str, port: int):
    db()[COL_ACCOUNTS].update_one(
        {"email": email},
        {"$set": {
            "email": email,
            "password": password,
            "server": server,
            "port": port,
            "updated_at": now_utc(),
        }},
        upsert=True,
    )


def delete_account(email: str):
    db()[COL_ACCOUNTS].delete_one({"email": email})


def get_account(email: str) -> Optional[Dict]:
    return db()[COL_ACCOUNTS].find_one({"email": email}, {"_id": 0})


# ---------------------------------------------------------------------------
# Mailbox processing status
# ---------------------------------------------------------------------------

def upsert_status(email: str, fields: Dict[str, Any]):
    db()[COL_STATUS].update_one(
        {"email": email},
        {"$set": {"email": email, "collection_name": sanitize(email), **fields}},
        upsert=True,
    )


def get_status(email: str) -> Optional[Dict]:
    return db()[COL_STATUS].find_one({"email": email}, {"_id": 0})


def get_all_statuses() -> List[Dict]:
    return list(db()[COL_STATUS].find({}, {"_id": 0}).sort("email", ASCENDING))


# ---------------------------------------------------------------------------
# Staging (raw fetched emails)
# ---------------------------------------------------------------------------

def staging_count(email: str, only_unclassified: bool = False) -> int:
    q = {"classified": {"$ne": True}} if only_unclassified else {}
    return db()[staging_col_name(email)].count_documents(q)


def iter_staging(email: str, batch_size: int = 25):
    """Yield only unclassified staging docs in batches (skip already-done emails on restart)."""
    all_docs = list(db()[staging_col_name(email)].find({"classified": {"$ne": True}}))
    for i in range(0, len(all_docs), batch_size):
        yield all_docs[i : i + batch_size]


def mark_staging_classified(email: str, message_ids: List[str]):
    """Stamp successfully classified staging docs so they are skipped on restart."""
    if not message_ids:
        return
    db()[staging_col_name(email)].update_many(
        {"message_id": {"$in": message_ids}},
        {"$set": {"classified": True}},
    )


def staging_to_dataframe(email: str):
    import pandas as pd
    docs = list(db()[staging_col_name(email)].find({}))
    if not docs:
        return pd.DataFrame()
    df = pd.DataFrame(docs)
    df["_id"] = df["_id"].astype(str)
    for col in df.columns:
        df[col] = df[col].apply(
            lambda v: "; ".join(str(x) for x in v) if isinstance(v, list) else v
        )
    return df


def get_staging_message_ids(email: str) -> List[str]:
    """Return all message_id values from staging for use before dropping the collection."""
    docs = db()[staging_col_name(email)].find({}, {"message_id": 1, "_id": 0})
    return [d["message_id"] for d in docs if d.get("message_id")]


def drop_staging(email: str):
    db()[staging_col_name(email)].drop()
    db()[sync_meta_col_name(email)].drop()


def upsert_sync_meta(email: str, meta: Dict):
    db()[sync_meta_col_name(email)].replace_one(
        {"email": email}, {**meta, "email": email}, upsert=True
    )


# ---------------------------------------------------------------------------
# Classification output collections
# ---------------------------------------------------------------------------

def export_prospects(
    mailbox: str = "",
    industry: str = "",
    job_title: str = "",
    status: str = "confirmed",   # "confirmed" | "skipped" | "all"
    include_generic: bool = False,
    limit: int = 5000,
) -> List[Dict]:
    """Return prospects for export with full field set."""
    q: Dict = {}
    if mailbox:
        q["$or"] = [{"source_mailbox": mailbox}, {"source_mailboxes": mailbox}]
    if industry:
        q["industry"] = industry
    if job_title:
        q["job_title"] = {"$regex": job_title, "$options": "i"}
    if not include_generic:
        q["generic"] = {"$ne": True}
    if status == "confirmed":
        q["checked"] = True
        q["skipped"] = False
    elif status == "skipped":
        q["skipped"] = True
    # "all" → no status filter (but generic still excluded unless include_generic)

    fields = {
        "_id": 0, "email": 1, "prospect_name": 1, "job_title": 1,
        "company": 1, "industry": 1, "webinar": 1, "phone": 1,
        "source_type": 1, "source_mailbox": 1, "source_mailboxes": 1,
        "original_sender_email": 1, "classified_at": 1,
    }
    return list(db()[COL_PROSPECT].find(q, fields).sort("classified_at", -1).limit(limit))


def query_collection(col_name: str, mailbox: str = "", sub_type: str = "", limit: int = 500) -> List[Dict]:
    q: Dict = {}
    if mailbox:
        q["source_mailbox"] = mailbox
    type_field = "removal_type" if col_name == COL_REMOVAL else "unavailable_type"
    if sub_type:
        q[type_field] = sub_type
    return list(
        db()[col_name].find(q, {"_id": 0}).sort("classified_at", -1).limit(limit)
    )


def collection_counts(col_name: str) -> Dict:
    pipeline = [{"$group": {"_id": "$source_mailbox", "count": {"$sum": 1}}}]
    return {r["_id"]: r["count"] for r in db()[col_name].aggregate(pipeline)}


def insert_removal(doc: Dict):
    db()[COL_REMOVAL].insert_one({**doc, "classified_at": now_utc()})


def insert_unavailable(doc: Dict):
    db()[COL_UNAVAIL].insert_one({**doc, "classified_at": now_utc()})


_PROSPECT_NO_OVERWRITE = {"email", "checked", "skipped", "classified_at", "source_mailbox", "source_mailboxes", "webinars"}

def insert_prospect(doc: Dict):
    # Upsert on email — never overwrite checked/skipped set by a reviewer
    email = doc.get("email", "").lower().strip()
    if not email:
        return
    db()[COL_PROSPECT].update_one(
        {"email": email},
        {
            "$setOnInsert": {"classified_at": now_utc(), "checked": False, "skipped": False},
            "$set": {k: v for k, v in doc.items() if v and k not in _PROSPECT_NO_OVERWRITE},
            "$addToSet": {
                "source_mailboxes": doc.get("source_mailbox", ""),
                "webinars": doc.get("webinar", ""),
            },
        },
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Prospect review queue
# ---------------------------------------------------------------------------

def get_all_prospects(
    checked: bool | None = None,
    skipped: bool | None = None,
    limit: int = 500,
) -> List[Dict]:
    q: Dict = {}
    if checked is not None:
        q["checked"] = checked
    if skipped is not None:
        q["skipped"] = skipped
    fields = {"_id": 1, "email": 1, "prospect_name": 1,
              "job_title": 1, "company": 1, "industry": 1,
              "source_mailbox": 1, "checked": 1, "skipped": 1, "generic": 1}
    return list(db()[COL_PROSPECT].find(q, fields).sort("classified_at", -1).limit(limit))


def update_prospect_fields(doc_id: str, fields: Dict):
    db()[COL_PROSPECT].update_one(
        {"_id": ObjectId(doc_id)},
        {"$set": fields},
    )


def mark_generic(doc_id: str):
    """Mark prospect as generic/irrelevant — checked and flagged so it's excluded from exports."""
    db()[COL_PROSPECT].update_one(
        {"_id": ObjectId(doc_id)},
        {"$set": {"generic": True, "checked": True, "skipped": True}},
    )


def search_prospects(
    job_title: str = "", industry: str = "", email: str = "", limit: int = 100
) -> List[Dict]:
    q: Dict = {}
    if job_title:
        q["job_title"] = {"$regex": job_title, "$options": "i"}
    if industry and industry != "All":
        q["industry"] = industry
    if email:
        q["email"] = {"$regex": email, "$options": "i"}
    fields = {"_id": 1, "email": 1, "prospect_name": 1,
              "job_title": 1, "company": 1, "industry": 1,
              "checked": 1, "skipped": 1, "generic": 1}
    return list(db()[COL_PROSPECT].find(q, fields).sort("classified_at", -1).limit(limit))


def _mailbox_query(email: str) -> Dict:
    """Match prospects belonging to a mailbox via either the scalar or array field."""
    return {"$or": [{"source_mailbox": email}, {"source_mailboxes": email}]}


def count_prospects(email: str, unchecked_only: bool = True) -> int:
    q: Dict = _mailbox_query(email)
    if unchecked_only:
        q["checked"] = False
    return db()[COL_PROSPECT].count_documents(q)


def get_next_prospect(email: str) -> Optional[Dict]:
    q = {**_mailbox_query(email), "checked": False}
    return db()[COL_PROSPECT].find_one(q)


def mark_checked(doc_id: str, enriched_fields: Dict):
    db()[COL_PROSPECT].update_one(
        {"_id": ObjectId(doc_id)},
        {"$set": {**enriched_fields, "checked": True, "skipped": False, "enriched_at": now_utc()}},
    )


def mark_skipped(doc_id: str):
    db()[COL_PROSPECT].update_one(
        {"_id": ObjectId(doc_id)},
        {"$set": {"checked": True, "skipped": True, "enriched_at": now_utc()}},
    )


# ---------------------------------------------------------------------------
# Ensure indexes
# ---------------------------------------------------------------------------

def ensure_indexes():
    db()[COL_ACCOUNTS].create_index([("email", ASCENDING)], unique=True)
    db()[COL_STATUS].create_index([("email", ASCENDING)], unique=True)
    db()[COL_PROSPECT].create_index([("email", ASCENDING)], unique=True)
    db()[COL_PROSPECT].create_index([("source_mailbox", ASCENDING), ("checked", ASCENDING)])
    db()[COL_REMOVAL].create_index([("email", ASCENDING), ("source_mailbox", ASCENDING)])
    db()[COL_UNAVAIL].create_index([("email", ASCENDING), ("source_mailbox", ASCENDING)])
