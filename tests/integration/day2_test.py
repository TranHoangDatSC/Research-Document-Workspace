"""Run on Windows with Python 3.11+. Standard library only. No secrets needed.

before: create and test; after: read SAME IDs after Compose down/up, no reseed.
failure: run while MongoDB is stopped; recovery: run after MongoDB restarts.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
from uuid import uuid4

from _auth_helper import build_cookie_opener, login

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'artifacts' / 'day-02'
STATE = OUTPUT / 'day-02-state.json'
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


def upload(project_id, name, payload, custom='{"language":"en","course":"Cloud"}'):
    boundary = 'day2-' + uuid4().hex
    chunks = []
    for key, value in {'tags': 'cloud,database', 'authors': 'Student', 'custom_metadata': custom}.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
    chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
    chunks.extend([payload, f'\r\n--{boundary}--\r\n'.encode()])
    return request(f'/projects/{project_id}/documents', 'POST', b''.join(chunks), f'multipart/form-data; boundary={boundary}')


def storage_evidence(document):
    # Run read-only checks inside web using its existing credentials, never output them.
    source = '''
import json, os
from uuid import UUID
from app.storage import postgres_connection, mongo_client, minio_client
from app.bootstrap import bucket_name
document_id = UUID(DOCUMENT_ID)
with postgres_connection() as c:
    row = c.execute("SELECT status, object_name FROM documents WHERE id=%s", (document_id,)).fetchone()
with mongo_client() as c:
    metadata = c[os.environ["MONGO_DB"]]["document_details"].find_one({"document_id": str(document_id)})
stat = minio_client().stat_object(bucket_name(), row[1])
print(json.dumps({"postgresql_status": row[0], "mongodb_document_id": metadata["document_id"], "bucket": bucket_name(), "object_name": row[1], "minio_size": stat.size}))
'''.replace('DOCUMENT_ID', repr(document['id']))
    process = subprocess.run(
        ['docker', 'compose', 'exec', '-T', 'web', 'python', '-'],
        input=source, text=True, capture_output=True, cwd=ROOT,
    )
    if process.returncode:
        raise RuntimeError('Storage evidence failed: ' + process.stderr[-800:])
    evidence = json.loads(process.stdout)
    require(evidence['postgresql_status'] == 'ready', 'PostgreSQL ready row')
    require(evidence['mongodb_document_id'] == document['id'], 'MongoDB metadata row')
    require(evidence['minio_size'] == document['size_bytes'], 'MinIO object size')
    (OUTPUT / 'day-02-storage-evidence.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps(evidence, indent=2))


def verify_saved(state, label):
    project = as_json('/projects/' + state['project_id'])
    require(project['id'] == state['project_id'], label + ' project')
    for index, saved in enumerate(state['documents']):
        current = as_json('/documents/' + saved['id'])
        require(current['status'] == 'ready' and current['sha256'] == saved['sha256'], label + f' metadata {index + 1}')
        require(current['tags'] == ['cloud', 'database'] and current['custom_metadata']['course'] == 'Cloud', label + f' flexible fields {index + 1}')
        status, payload = request('/documents/' + saved['id'] + '/download')
        require(status == 200 and hashlib.sha256(payload).hexdigest() == saved['sha256'], label + f' download SHA256 {index + 1}')
        (OUTPUT / f'day-02-{label}-download-{index + 1}.txt').write_bytes(payload)
    storage_evidence(state['documents'][0])


def before():
    require(as_json('/health/live')['status'] == 'alive', 'Liveness')
    require(as_json('/health/ready')['status'] == 'ready', 'Readiness')
    project = as_json('/projects', 'POST', {'name': 'Day 2 ' + uuid4().hex[:8], 'description': 'Automated integration check'}, 201)
    pid = project['id']
    require(as_json('/projects/' + pid)['id'] == pid, 'Create/read project')
    require(any(p['id'] == pid for p in as_json('/projects?limit=100')), 'List projects')
    status, _ = request('/projects/' + str(uuid4()))
    require(status == 404, 'Missing project 404')
    status, _ = request('/projects', 'POST', b'{"name":"   "}', 'application/json')
    require(status == 422, 'Blank name 422')
    payload = (ROOT / 'samples/day2-sample.txt').read_bytes()
    documents = []
    for data in (payload, payload + b'\nSecond file, same original filename.\n'):
        status, body = upload(pid, 'day2-sample.txt', data)
        require(status == 201, 'Upload 201')
        document = json.loads(body)
        require(document['sha256'] == hashlib.sha256(data).hexdigest(), 'Upload SHA256')
        documents.append(document)
    require(documents[0]['object_name'] != documents[1]['object_name'], 'Duplicate filenames use distinct objects')
    checks = [
        (str(uuid4()), 'missing.txt', b'hello', '{}', 404, 'Upload missing project'),
        # Images have the smallest default limit (25 MiB, app/file_types.py).
        (pid, 'large.png', b'x' * (25 * 1024 * 1024 + 1), '{}', 413, 'Oversized file'),
        (pid, 'blocked.exe', b'hello', '{}', 415, 'Unsupported extension'),
        (pid, 'empty.txt', b'', '{}', 422, 'Empty file'),
        (pid, 'bad-json.txt', b'hello', 'not-json', 422, 'Malformed metadata'),
    ]
    for target, name, data, custom, expected, label in checks:
        status, _ = upload(target, name, data, custom)
        require(status == expected, f'{label} {expected}')
    listed = as_json(f'/projects/{pid}/documents?limit=100')
    require(len(listed) == 2 and all(x['status'] == 'ready' for x in listed), 'Rejected requests create no extra rows')
    require(request('/documents/' + str(uuid4()))[0] == 404, 'Missing document 404')
    require(request('/documents/' + str(uuid4()) + '/download')[0] == 404, 'Missing download 404')
    state = {'project_id': pid, 'documents': documents}
    STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
    verify_saved(state, 'before')


def failure(state):
    require(as_json('/health/live')['status'] == 'alive', 'Live during MongoDB stop')
    ready = as_json('/health/ready', expected=503)
    require(ready['services']['mongodb'] == 'down', 'Readiness MongoDB down 503')
    name = 'failure-' + uuid4().hex + '.txt'
    status, _ = upload(state['project_id'], name, b'Controlled failure test')
    require(status == 503, 'Upload during MongoDB outage 503')
    rows = as_json(f'/projects/{state["project_id"]}/documents?limit=100')
    failed = [row for row in rows if row['original_name'] == name]
    require(len(failed) == 1 and failed[0]['status'] == 'failed', 'Interrupted upload marked failed')
    require(request('/documents/' + failed[0]['id'] + '/download')[0] == 409, 'Failed upload cannot download 409')
    state['failure_document'] = failed[0]
    STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')


def recovery(state):
    require(as_json('/health/ready')['status'] == 'ready', 'Readiness recovered')
    failed = state['failure_document']
    source = '''
import os
from app.storage import mongo_client, minio_client
from app.bootstrap import bucket_name
from minio.error import S3Error
with mongo_client() as c:
    if c[os.environ["MONGO_DB"]]["document_details"].find_one({"document_id": DOC_ID}) is not None:
        raise RuntimeError("Unexpected MongoDB metadata for failed upload")
try:
    minio_client().stat_object(bucket_name(), OBJECT_NAME)
except S3Error as exc:
    if exc.code not in ("NoSuchKey", "NoSuchObject", "NotFound"):
        raise
else:
    raise RuntimeError("Orphan MinIO object remains")
print("Failed upload cleanup: PASS")
'''.replace('DOC_ID', repr(failed['id'])).replace('OBJECT_NAME', repr(failed['object_name']))
    process = subprocess.run(['docker', 'compose', 'exec', '-T', 'web', 'python', '-'], input=source, text=True, capture_output=True, cwd=ROOT)
    require(process.returncode == 0, 'Failed upload leaves no MongoDB metadata or MinIO object')
    verify_saved(state, 'recovery')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['before', 'after', 'failure', 'recovery'])
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = OUTPUT / f'day-02-{args.phase}-result.txt'
    urllib.request.install_opener(build_cookie_opener())
    try:
        login(BASE)
        require(True, 'Admin login')
        if args.phase == 'before':
            before()
        else:
            state = json.loads(STATE.read_text(encoding='utf-8'))
            if args.phase == 'after':
                require(as_json('/health/ready')['status'] == 'ready', 'Readiness after recreation')
                verify_saved(state, 'after')
            elif args.phase == 'failure':
                failure(state)
            else:
                recovery(state)
        results.append('DAY 2 ' + args.phase.upper() + ': PASS')
        print(results[-1])
    except Exception as exc:
        results.append('FAIL: ' + str(exc))
        print(results[-1], file=sys.stderr)
        report.write_text('\n'.join(results) + '\n', encoding='utf-8')
        sys.exit(1)
    report.write_text('\n'.join(results) + '\n', encoding='utf-8')
