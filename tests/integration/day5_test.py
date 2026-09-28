"""Run on Windows with Python 3.11+. Standard library only. No secrets needed.

check: create a project, upload .txt/.pdf/.docx, extract each, verify the result
is persisted (a fresh HTTP GET and an independent direct MongoDB read both see
it), and verify the extraction error paths (corrupt file, encrypted PDF,
missing document).
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
from uuid import uuid4

from _auth_helper import build_cookie_opener, login

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / 'samples'
OUTPUT = ROOT / 'artifacts' / 'day-05'
BASE = 'http://127.0.0.1:8001'
results = []


def require(condition, name):
    if not condition:
        raise RuntimeError(name)
    results.append(name + ': PASS')
    print(results[-1], flush=True)


def request(path, method='GET', data=None, content_type=None):
    headers = {'Content-Type': content_type} if content_type else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def as_json(path, method='GET', value=None, expected=200):
    data = json.dumps(value).encode() if value is not None else None
    status, body = request(path, method, data, 'application/json' if data is not None else None)
    if status != expected:
        raise RuntimeError(f'{method} {path}: expected {expected}, got {status}: {body[:300]!r}')
    return json.loads(body)


def upload(project_id, name, payload, content_type='application/octet-stream'):
    boundary = 'day5-' + uuid4().hex
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{name}"\r\nContent-Type: {content_type}\r\n\r\n'
    ).encode()
    tail = f'\r\n--{boundary}--\r\n'.encode()
    body = head + payload + tail
    return request(f'/projects/{project_id}/documents', 'POST', body, f'multipart/form-data; boundary={boundary}')


def run_in_web(source):
    # Read-only checks executed inside web using its existing credentials.
    process = subprocess.run(
        ['docker', 'compose', 'exec', '-T', 'web', 'python', '-'],
        input=source, text=True, capture_output=True, cwd=ROOT,
    )
    if process.returncode:
        raise RuntimeError('web exec failed: ' + process.stderr[-800:])
    return json.loads(process.stdout)


def mongo_details(document_id):
    source = '''
import json, os
from app.storage import mongo_client
with mongo_client() as c:
    doc = c[os.environ["MONGO_DB"]]["document_details"].find_one(
        {"document_id": DOCUMENT_ID}, {"_id": 0}
    )
print(json.dumps(doc))
'''.replace('DOCUMENT_ID', repr(document_id))
    return run_in_web(source)


def mongo_count(document_id):
    source = '''
import json, os
from app.storage import mongo_client
with mongo_client() as c:
    n = c[os.environ["MONGO_DB"]]["document_details"].count_documents(
        {"document_id": DOCUMENT_ID}
    )
print(json.dumps(n))
'''.replace('DOCUMENT_ID', repr(document_id))
    return run_in_web(source)


def check():
    require(as_json('/health/live')['status'] == 'alive', 'Liveness')
    require(as_json('/health/ready')['status'] == 'ready', 'Readiness')

    project = as_json('/projects', 'POST', {'name': 'Day 5 ' + uuid4().hex[:8]}, 201)
    pid = project['id']

    txt_text = 'Xin chào Cloud Docker - kiểm thử trích xuất.'
    cases = [
        ('sample.txt', txt_text.encode('utf-8'), 'text/plain', 'plain_text', txt_text),
        ('sample.pdf', (SAMPLES / 'day5-sample.pdf').read_bytes(), 'application/pdf',
         'pdf_text', 'Day 5 sample PDF for extraction testing.'),
        ('sample.docx', (SAMPLES / 'day5-sample.docx').read_bytes(),
         'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
         'docx_text', 'Ngay 5 - tai lieu mau DOCX.\nXin chào Cloud và Docker.'),
    ]

    txt_document_id = None
    for filename, payload, content_type, expected_method, expected_text in cases:
        status, body = upload(pid, filename, payload, content_type)
        require(status == 201, f'Upload {filename} 201')
        document = json.loads(body)
        require(document['extracted_text'] is None, f'{filename} has no extracted_text before extraction')
        if filename == 'sample.txt':
            txt_document_id = document['id']

        status, body = request(f'/documents/{document["id"]}/extract', 'POST')
        require(status == 200, f'Extract {filename} 200')
        extracted = json.loads(body)['extracted_text']
        require(extracted['method'] == expected_method, f'{filename} extraction method')
        require(extracted['text'] == expected_text, f'{filename} extracted text matches source')
        require(extracted['character_count'] == len(expected_text), f'{filename} character count')
        require(extracted['truncated'] is False, f'{filename} not truncated')

        # Persistence, witness 1: a fresh HTTP GET (new request) sees the same result.
        refetched = as_json(f'/documents/{document["id"]}')
        require(refetched['extracted_text'] == extracted, f'{filename} extraction persisted across GET')

        # Persistence, witness 2: read MongoDB directly, bypassing the API entirely.
        direct = mongo_details(document['id'])
        require(direct['extracted_text'] == extracted, f'{filename} extraction persisted in MongoDB (direct read)')

    # Re-extraction overwrites the same MongoDB row rather than creating a new one.
    status, body = request(f'/documents/{txt_document_id}/extract', 'POST')
    require(status == 200, 'Re-extract sample.txt 200')
    require(mongo_count(txt_document_id) == 1, 'Re-extraction leaves exactly one MongoDB row')

    # Error path: extension is accepted at upload (content is not sniffed), but a
    # corrupt PDF must fail extraction cleanly instead of crashing the request.
    status, body = upload(pid, 'corrupt.pdf', b'not actually a pdf file', 'application/pdf')
    require(status == 201, 'Corrupt PDF still uploads (extension-only check)')
    corrupt_id = json.loads(body)['id']
    status, body = request(f'/documents/{corrupt_id}/extract', 'POST')
    require(status == 422, 'Corrupt PDF extraction returns 422')
    direct = mongo_details(corrupt_id)
    require(direct['extracted_text'] is None, 'Corrupt PDF leaves no extracted_text in MongoDB')

    # Error path: a password-protected PDF is a readable file but not extractable text.
    status, body = upload(pid, 'encrypted.pdf', (SAMPLES / 'day5-encrypted-sample.pdf').read_bytes(), 'application/pdf')
    require(status == 201, 'Encrypted PDF still uploads')
    encrypted_id = json.loads(body)['id']
    status, body = request(f'/documents/{encrypted_id}/extract', 'POST')
    require(status == 422, 'Encrypted PDF extraction returns 422')
    direct = mongo_details(encrypted_id)
    require(direct['extracted_text'] is None, 'Encrypted PDF leaves no extracted_text in MongoDB')

    # Error path: extracting a document that was never uploaded.
    status, _ = request(f'/documents/{uuid4()}/extract', 'POST')
    require(status == 404, 'Extract missing document 404')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', nargs='?', default='check', choices=['check'])
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = OUTPUT / f'day-05-{args.phase}-result.txt'
    urllib.request.install_opener(build_cookie_opener())
    try:
        login(BASE)
        require(True, 'Admin login')
        check()
        results.append('DAY 5 ' + args.phase.upper() + ': PASS')
        print(results[-1])
    except Exception as exc:
        results.append('FAIL: ' + str(exc))
        print(results[-1], file=sys.stderr)
        report.write_text('\n'.join(results) + '\n', encoding='utf-8')
        sys.exit(1)
    report.write_text('\n'.join(results) + '\n', encoding='utf-8')
