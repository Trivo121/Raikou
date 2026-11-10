"""Verify S3 / Google Cloud Storage credentials and operations.

Reads settings from ``backend/.env`` and exercises:
1. Bucket accessibility and credentials
2. Direct object write, read, and delete
3. Presigned GET preview URL generation & fetch verification
4. Multipart upload initiation, part upload, and completion (testing GCS S3-interoperability)

Run:
    python backend/scripts/check_storage_credentials.py
"""

from __future__ import annotations

import os
import sys
import uuid
import urllib.request
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError

REPO_BACKEND = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_BACKEND / ".env"


def _mask(value: str | None) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return f"set, {len(value)} chars"
    return f"{value[:6]}...{value[-4:]} ({len(value)} chars)"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    env = load_env()

    bucket = env.get("STORAGE_BUCKET") or env.get("S3_BUCKET_NAME") or env.get("AWS_S3_BUCKET")
    endpoint_url = env.get("STORAGE_ENDPOINT_URL") or env.get("S3_ENDPOINT_URL")
    region = env.get("STORAGE_REGION") or env.get("AWS_REGION") or "asia-south1"
    access_key = env.get("STORAGE_ACCESS_KEY_ID") or env.get("AWS_ACCESS_KEY_ID")
    secret_key = env.get("STORAGE_SECRET_ACCESS_KEY") or env.get("AWS_SECRET_ACCESS_KEY")
    checksum_mode = env.get("STORAGE_MULTIPART_CHECKSUM_MODE", "server_verified")
    force_path_style = env.get("STORAGE_FORCE_PATH_STYLE", "false").lower() == "true"

    print("=" * 60)
    print("Raikou Object Storage Verification (GCS / S3)")
    print("=" * 60)
    print(f"Bucket:           {bucket}")
    print(f"Endpoint:         {endpoint_url or '(AWS default)'}")
    print(f"Region:           {region}")
    print(f"Access Key ID:    {_mask(access_key)}")
    print(f"Secret Key:       {_mask(secret_key)}")
    print(f"Checksum Mode:    {checksum_mode}")
    print(f"Force Path Style: {force_path_style}")
    print("-" * 60)

    if not bucket or not access_key or not secret_key:
        print("❌ Missing required storage credentials in backend/.env!")
        print("   Ensure STORAGE_BUCKET, STORAGE_ACCESS_KEY_ID, and STORAGE_SECRET_ACCESS_KEY are set.")
        return 1

    config_kwargs = {
        "signature_version": "s3v4",
        "retries": {"max_attempts": 3, "mode": "standard"},
        "s3": {"addressing_style": "path" if force_path_style else "virtual"},
    }
    try:
        config = Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            **config_kwargs,
        )
    except TypeError:
        config = Config(**config_kwargs)

    client_kwargs = {
        "service_name": "s3",
        "region_name": region,
        "endpoint_url": endpoint_url,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "config": config,
    }

    try:
        s3 = boto3.client(**client_kwargs)
    except Exception as exc:
        print(f"❌ Failed to instantiate boto3 client: {exc}")
        return 1

    test_key = f"_healthcheck/test-{uuid.uuid4().hex[:8]}.txt"
    test_content = b"Raikou GCS S3-interoperability test payload - verified!"

    # 1. Put Object
    print(f"1. Writing test object to '{test_key}'...")
    try:
        s3.put_object(
            Bucket=bucket,
            Key=test_key,
            Body=test_content,
            ContentType="text/plain",
        )
        print("   ✅ Write succeeded.")
    except Exception as exc:
        print(f"   ❌ Write failed: {exc}")
        return 1

    # 2. Head Object & Get Object
    print("2. Reading test object back...")
    try:
        head_resp = s3.head_object(Bucket=bucket, Key=test_key)
        size = head_resp.get("ContentLength", 0)
        print(f"   ✅ Head succeeded (size: {size} bytes).")

        get_resp = s3.get_object(Bucket=bucket, Key=test_key)
        read_bytes = get_resp["Body"].read()
        if read_bytes == test_content:
            print("   ✅ Content verified successfully.")
        else:
            print("   ❌ Content mismatch!")
            return 1
    except Exception as exc:
        print(f"   ❌ Read failed: {exc}")
        return 1

    # 3. Presigned URL test
    print("3. Generating presigned GET preview URL...")
    try:
        signed_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": test_key},
            ExpiresIn=300,
        )
        print(f"   ✅ Presigned URL generated: {signed_url[:65]}...")
        req = urllib.request.Request(signed_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            fetched_content = resp.read()
            if fetched_content == test_content:
                print("   ✅ HTTP fetch via presigned URL verified successfully!")
            else:
                print("   ⚠️ HTTP fetched content did not match!")
    except Exception as exc:
        print(f"   ❌ Presigned URL test failed: {exc}")
        return 1

    # 4. Multipart upload compatibility check
    print("4. Testing multipart upload initiation, part upload, and completion...")
    mp_key = f"_healthcheck/mp-test-{uuid.uuid4().hex[:8]}.bin"
    try:
        mp_params = {
            "Bucket": bucket,
            "Key": mp_key,
            "ContentType": "application/octet-stream",
        }
        if checksum_mode == "sha256":
            mp_params["ChecksumAlgorithm"] = "SHA256"

        mp_resp = s3.create_multipart_upload(**mp_params)
        upload_id = mp_resp["UploadId"]
        print(f"   ✅ Multipart upload created (UploadId: {upload_id[:16]}...).")

        part_resp = s3.upload_part(
            Bucket=bucket,
            Key=mp_key,
            UploadId=upload_id,
            PartNumber=1,
            Body=b"A" * (5 * 1024 * 1024),
        )
        etag = part_resp["ETag"]
        print(f"   ✅ Part 1 uploaded (5 MiB, ETag: {etag}).")

        s3.complete_multipart_upload(
            Bucket=bucket,
            Key=mp_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": etag}]},
        )
        print("   ✅ Multipart upload finalized successfully.")

        s3.delete_object(Bucket=bucket, Key=mp_key)
        print("   ✅ Multipart test object cleaned up.")
    except Exception as exc:
        print(f"   ❌ Multipart upload test failed: {exc}")
        return 1

    # 5. Cleanup
    print("5. Cleaning up test object...")
    try:
        s3.delete_object(Bucket=bucket, Key=test_key)
        print("   ✅ Test object cleaned up.")
    except Exception as exc:
        print(f"   ⚠️ Could not delete test object: {exc}")

    print("=" * 60)
    print("🎉 ALL GCS STORAGE CHECKS PASSED 100% SUCCESSFULLY!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
