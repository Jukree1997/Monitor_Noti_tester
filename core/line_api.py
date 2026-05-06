from __future__ import annotations
import cv2
import numpy as np
import requests
import boto3


def send_text(token: str, target_id: str, message: str) -> tuple[bool, str]:
    """Send a text message via LINE Messaging API push."""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "to": target_id,
        "messages": [{"type": "text", "text": message}],
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        if resp.status_code == 200:
            return True, "200 OK"
        return False, f"{resp.status_code} {resp.text[:100]}"
    except requests.RequestException as e:
        return False, f"Error: {e}"


def send_text_and_image(token: str, target_id: str, message: str,
                        frame: np.ndarray, s3_config: dict) -> tuple[bool, str]:
    """Send text + snapshot image via LINE. Uploads frame to S3 first."""
    # Upload to S3
    image_url = _upload_to_s3(frame, s3_config)
    if image_url is None:
        # Fallback to text only
        success, status = send_text(token, target_id, message)
        return success, f"{status} (image upload failed)"

    # Send both text + image in one push
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "to": target_id,
        "messages": [
            {"type": "text", "text": message},
            {
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            },
        ],
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        if resp.status_code == 200:
            return True, "200 OK (text + image)"
        return False, f"{resp.status_code} {resp.text[:100]}"
    except requests.RequestException as e:
        return False, f"Error: {e}"


def _upload_to_s3(frame: np.ndarray, s3_config: dict) -> str | None:
    """Upload frame to S3 and return pre-signed URL. Returns None on failure."""
    try:
        bucket = s3_config["bucket"]
        region = s3_config["region"]
        access_key = s3_config["access_key"]
        secret_key = s3_config["secret_key"]
        expiry = s3_config.get("expiry", 600)

        s3 = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

        # Encode frame as JPEG
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])

        # Upload (overwrites previous)
        key = "baksters/alert_snapshot.jpg"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=buf.tobytes(),
            ContentType="image/jpeg",
        )

        # Generate pre-signed URL
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expiry,
        )
        return url

    except Exception:
        return None
