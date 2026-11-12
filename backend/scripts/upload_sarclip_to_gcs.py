"""Resumable and fault-tolerant SARCLIP upload to Google Cloud Storage.

Uploads ``SARCLIP-GeoRS-ViT-L-14.pt`` (~5.1 GB) in 32 MiB parts with automatic
retries and resumption. If the connection drops at any point, re-running this
script simply continues from the last successfully uploaded part.

Run:
    python backend/scripts/upload_sarclip_to_gcs.py
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND_DIR / ".env"

LOCAL_MODEL_PATH = REPO_ROOT / "SARVLM" / "SARCLIP-GeoRS-ViT-L-14.pt"
REMOTE_KEY = "models/SARCLIP-GeoRS-ViT-L-14.pt"
PART_SIZE = 32 * 1024 * 1024  # 32 MiB parts
STATE_FILE = BACKEND_DIR / ".sarclip_upload_state.json"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_s3_client(env: dict[str, str]):
    bucket = env.get("STORAGE_BUCKET") or "raikou-sar-uploads-mumbai"
    endpoint_url = env.get("STORAGE_ENDPOINT_URL") or "https://storage.googleapis.com"
    region = env.get("STORAGE_REGION") or "asia-south1"
    access_key = env.get("STORAGE_ACCESS_KEY_ID")
    secret_key = env.get("STORAGE_SECRET_ACCESS_KEY")

    config = Config(
        signature_version="s3v4",
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        connect_timeout=30,
        read_timeout=300,
        retries={"max_attempts": 5, "mode": "standard"},
        s3={"addressing_style": "virtual"},
    )

    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=config,
    )
    return client, bucket


def list_all_parts(s3, bucket: str, key: str, upload_id: str) -> dict[int, str]:
    existing_parts: dict[int, str] = {}
    marker = 0
    while True:
        kwargs = {"Bucket": bucket, "Key": key, "UploadId": upload_id}
        if marker:
            kwargs["PartNumberMarker"] = marker
        resp = s3.list_parts(**kwargs)
        for p in resp.get("Parts", []):
            existing_parts[p["PartNumber"]] = p["ETag"]
        if not resp.get("IsTruncated"):
            break
        marker = resp.get("NextPartNumberMarker", 0)
    return existing_parts


def main() -> int:
    env = load_env()
    s3, bucket = get_s3_client(env)

    if not LOCAL_MODEL_PATH.is_file():
        print(f"❌ Local model file not found at: {LOCAL_MODEL_PATH}")
        return 1

    file_size = LOCAL_MODEL_PATH.stat().st_size
    total_parts = math.ceil(file_size / PART_SIZE)

    print("=" * 65)
    print("Resumable SARCLIP Upload to Google Cloud Storage")
    print("=" * 65)
    print(f"Local file:   {LOCAL_MODEL_PATH} ({file_size / (1024**3):.2f} GB)")
    print(f"Target:       gs://{bucket}/{REMOTE_KEY}")
    print(f"Part size:    {PART_SIZE // (1024*1024)} MiB (total {total_parts} parts)")
    print("-" * 65)

    # 1. Check if complete object already exists
    try:
        head = s3.head_object(Bucket=bucket, Key=REMOTE_KEY)
        if head.get("ContentLength") == file_size:
            print(f"✅ Full model already exists in gs://{bucket}/{REMOTE_KEY} ({file_size} bytes).")
            print("Done!")
            return 0
    except Exception:
        pass

    # 2. Get or create multipart upload
    upload_id = None
    if STATE_FILE.is_file():
        try:
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if saved.get("key") == REMOTE_KEY and saved.get("file_size") == file_size:
                upload_id = saved.get("upload_id")
        except Exception:
            pass

    # Verify upload_id is valid on GCS
    existing_parts: dict[int, str] = {}
    if upload_id:
        try:
            existing_parts = list_all_parts(s3, bucket, REMOTE_KEY, upload_id)
            print(f"🔄 Resuming upload {upload_id[:16]}... ({len(existing_parts)}/{total_parts} parts already uploaded)")
        except Exception as exc:
            print(f"⚠️ Previous upload session expired or unavailable ({exc}). Starting fresh.")
            upload_id = None

    if not upload_id:
        print("Initiating new multipart upload...")
        mp = s3.create_multipart_upload(
            Bucket=bucket,
            Key=REMOTE_KEY,
            ContentType="application/octet-stream",
        )
        upload_id = mp["UploadId"]
        STATE_FILE.write_text(
            json.dumps({"upload_id": upload_id, "key": REMOTE_KEY, "file_size": file_size}),
            encoding="utf-8",
        )
        print(f"Created upload: {upload_id[:16]}...")

    # 3. Upload missing parts
    completed_parts = [{"PartNumber": num, "ETag": etag} for num, etag in sorted(existing_parts.items())]

    start_time = time.time()
    session_bytes = 0
    uploaded_bytes = sum(
        PART_SIZE if num < total_parts else (file_size - (num - 1) * PART_SIZE)
        for num in existing_parts
    )

    with open(LOCAL_MODEL_PATH, "rb") as f:
        for part_num in range(1, total_parts + 1):
            if part_num in existing_parts:
                continue

            offset = (part_num - 1) * PART_SIZE
            chunk_length = min(PART_SIZE, file_size - offset)
            f.seek(offset)
            chunk = f.read(chunk_length)

            # Retry loop for each part (up to 30 attempts, surviving up to ~10 mins of drop)
            max_retries = 30
            part_etag = None
            for attempt in range(1, max_retries + 1):
                try:
                    resp = s3.upload_part(
                        Bucket=bucket,
                        Key=REMOTE_KEY,
                        UploadId=upload_id,
                        PartNumber=part_num,
                        Body=chunk,
                    )
                    part_etag = resp["ETag"]
                    break
                except (BotoCoreError, ClientError, Exception) as exc:
                    wait_s = min(attempt * 5, 30)
                    print(
                        f"\n⚠️ Part {part_num}/{total_parts} attempt {attempt}/{max_retries} "
                        f"failed ({exc}). Recreating client & retrying in {wait_s}s..."
                    )
                    time.sleep(wait_s)
                    s3, bucket = get_s3_client(env)

            if not part_etag:
                print(f"\n❌ Failed to upload part {part_num} after {max_retries} attempts.")
                print("Run this script again to resume from this part.")
                return 1

            completed_parts.append({"PartNumber": part_num, "ETag": part_etag})
            uploaded_bytes += chunk_length
            session_bytes += chunk_length
            elapsed = time.time() - start_time
            speed = (session_bytes / (1024 * 1024)) / max(elapsed, 0.001)
            pct = (uploaded_bytes / file_size) * 100
            print(
                f"\rPart {part_num}/{total_parts} OK | "
                f"{uploaded_bytes / (1024*1024):.1f}/{file_size / (1024*1024):.1f} MB ({pct:.1f}%) | "
                f"{speed:.2f} MB/s",
                end="",
                flush=True,
            )

    # 4. Finalize
    print("\nCompleting multipart upload on GCS...")
    completed_parts.sort(key=lambda p: p["PartNumber"])
    s3.complete_multipart_upload(
        Bucket=bucket,
        Key=REMOTE_KEY,
        UploadId=upload_id,
        MultipartUpload={"Parts": completed_parts},
    )

    if STATE_FILE.is_file():
        STATE_FILE.unlink(missing_ok=True)

    print("=" * 65)
    print("🎉 SARCLIP weights upload 100% COMPLETE!")
    print(f"Stored at: gs://{bucket}/{REMOTE_KEY}")
    print("=" * 65)
    return 0


if __name__ == "__main__":
    sys.exit(main())
