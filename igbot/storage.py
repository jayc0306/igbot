"""Instagram API는 공개 URL에서 미디어를 가져가므로 R2(S3 호환)에 올려 URL을 만든다."""
import mimetypes
from pathlib import Path

import boto3

import config

_s3 = boto3.client(
    "s3",
    endpoint_url=config.S3_ENDPOINT_URL,
    aws_access_key_id=config.S3_ACCESS_KEY_ID,
    aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
    region_name="auto",
)


def upload(path: Path, key: str) -> str:
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    _s3.upload_file(str(path), config.S3_BUCKET, key, ExtraArgs={"ContentType": ctype})
    return f"{config.S3_PUBLIC_BASE_URL}/{key}"


def delete(keys: list[str]):
    if not keys:
        return
    _s3.delete_objects(Bucket=config.S3_BUCKET,
                       Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})
