"""Run from a machine OUTSIDE the VPS (home Wi-Fi, phone on 4G, a friend's
laptop) after DNS and HTTPS are live. Standard library only: no Docker or SSH
access needed on the machine running this script.

Usage:
    python deploy/external-smoke-test.py https://your-domain.example.com
"""
import argparse
import hashlib
import json
from uuid import uuid4
import urllib.error
import urllib.request


def request(base, path, method="GET", data=None, content_type=None):
    headers = {"Content-Type": content_type} if content_type else {}
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        # A successful HTTPS response here already proves a valid, trusted
        # certificate chain: urllib rejects self-signed/expired certs by default.
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def upload(base, project_id, name, payload):
    boundary = "smoke-" + uuid4().hex
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{name}"\r\nContent-Type: text/plain\r\n\r\n'
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + payload + tail
    return request(
        base, f"/projects/{project_id}/documents", "POST", body,
        f"multipart/form-data; boundary={boundary}",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", help="e.g. https://your-domain.example.com")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if not base.startswith("https://"):
        raise SystemExit("Use an https:// URL — this must prove the public TLS path, not localhost.")

    status, body = request(base, "/health/ready")
    assert status == 200 and json.loads(body)["status"] == "ready", \
        f"/health/ready failed: {status} {body[:200]!r}"
    print("HTTPS + readiness: PASS")

    status, body = request(
        base, "/projects", "POST",
        json.dumps({"name": "External smoke test"}).encode(), "application/json",
    )
    assert status == 201, f"create project failed: {status} {body[:200]!r}"
    project_id = json.loads(body)["id"]
    print("Create project over public HTTPS: PASS")

    payload = f"External smoke test {uuid4()}".encode("utf-8")
    status, body = upload(base, project_id, "smoke.txt", payload)
    assert status == 201, f"upload failed: {status} {body[:200]!r}"
    document = json.loads(body)
    assert document["sha256"] == hashlib.sha256(payload).hexdigest(), "SHA-256 mismatch"
    print("Upload over public HTTPS: PASS")

    status, body = request(base, f'/documents/{document["id"]}/download')
    assert status == 200 and body == payload, "download content mismatch"
    print("Download over public HTTPS: PASS")

    print("EXTERNAL SMOKE TEST: PASS")


if __name__ == "__main__":
    main()
