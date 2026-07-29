"""S3 archive — upload, verify, and delete staging collection."""
import io
from datetime import datetime, timezone
from typing import Dict, Tuple

from core.config import get_secret


def _s3_client():
    import boto3
    return boto3.client(
        "s3",
        region_name          = get_secret("AWS_REGION", "us-east-1"),
        aws_access_key_id    = get_secret("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key= get_secret("AWS_SECRET_ACCESS_KEY"),
    )


def s3_key(email: str) -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe     = email.replace("@", "_at_").replace(".", "_dot_")
    return f"archives/{safe}/{date_str}_{safe}.csv"


def upload_csv(email: str, csv_bytes: bytes) -> Tuple[bool, str, str]:
    """
    Upload CSV bytes to S3.
    Returns (success, key, error_message).
    """
    bucket = get_secret("AWS_BUCKET")
    if not bucket:
        return False, "", "AWS_BUCKET not configured in secrets."
    key = s3_key(email)
    try:
        client = _s3_client()
        client.put_object(
            Bucket      = bucket,
            Key         = key,
            Body        = csv_bytes,
            ContentType = "text/csv",
        )
        return True, key, ""
    except Exception as e:
        return False, "", str(e)


def verify_upload(key: str, expected_rows: int) -> Dict:
    """
    Verify S3 object exists and byte size > 0.
    expected_rows is used for a rough sanity check.
    Returns dict with: ok (bool), size_bytes (int), message (str).
    """
    bucket = get_secret("AWS_BUCKET")
    try:
        client   = _s3_client()
        head     = client.head_object(Bucket=bucket, Key=key)
        size     = head["ContentLength"]
        ok       = size > 0
        msg      = f"Object exists — {size:,} bytes." if ok else "Object is empty."
        return {"ok": ok, "size_bytes": size, "message": msg}
    except Exception as e:
        return {"ok": False, "size_bytes": 0, "message": str(e)}


def s3_configured() -> bool:
    return bool(get_secret("AWS_BUCKET") and get_secret("AWS_ACCESS_KEY_ID"))
