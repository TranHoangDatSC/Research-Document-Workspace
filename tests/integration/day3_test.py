"""Host-side standard library tests. Only deletes fixtures created by this script."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4
from _auth_helper import build_cookie_opener, login
ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'artifacts' / 'day-03'
STATE = OUTPUT / 'state.json'
BASE = 'http://127.0.0.1:8001'
RESULTS = []
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
OPENER = build_cookie_opener(NoRedirect)
def check(value, label):
    if not value:
        raise RuntimeError(label)
    RESULTS.append(label + ': PASS')
    print(RESULTS[-1], flush=True)
def request(path, method='GET', body=None, content_type=None):
    headers = {'Content-Type': content_type} if content_type else {}
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        r = OPENER.open(req, timeout=60)
    except urllib.error.HTTPError as exc:
        r = exc
    with r:
        return r.code, r.read(), r.headers
def form(path, fields):
    return request(path, 'POST', urllib.parse.urlencode(fields).encode(), 'application/x-www-form-urlencoded')
def get_json(path):
    status, body, _ = request(path)
    check(status == 200, 'GET ' + path)
    return json.loads(body)
def upload(pid, payload):
    boundary = 'day3-' + uuid4().hex
    parts = []
    for key, value in {'tags':'day3,cloud','authors':'Cloud Student','custom_metadata':'{"checkpoint":"day3"}'}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="day3.txt"\r\nContent-Type: text/plain\r\n\r\n'.encode())
    parts.extend([payload, f'\r\n--{boundary}--\r\n'.encode()])
    status, _, headers = request(f'/ui/projects/{pid}/documents', 'POST', b''.join(parts), 'multipart/form-data; boundary=' + boundary)
    check(status == 303, 'Upload UI redirect')
    return get_json('/documents/' + headers['Location'].rsplit('/', 1)[-1])
def storage(doc, absent=False):
    source = '''
import json, os
from uuid import UUID
from minio.error import S3Error
from app.storage import postgres_connection, mongo_client, minio_client
from app.bootstrap import bucket_name
info = json.loads(INFO)
with postgres_connection() as c:
    row = c.execute('SELECT status FROM documents WHERE id=%s', (UUID(info['id']),)).fetchone()
with mongo_client() as c:
    metadata = c[os.environ['MONGO_DB']]['document_details'].find_one({'document_id': info['id']})
try:
    size = minio_client().stat_object(bucket_name(), info['object_name']).size
except S3Error as exc:
    if exc.code != 'NoSuchKey':
        raise
    size = None
print(json.dumps({'status': row[0] if row else None, 'mongo': metadata is not None, 'size': size}))
'''.replace('INFO', repr(json.dumps({'id':doc['id'], 'object_name':doc['object_name']})))
    result = subprocess.run(['docker','compose','exec','-T','web','python','-'], cwd=ROOT, input=source, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Storage check failed. Read docker compose logs --tail 100 web.')
    data = json.loads(result.stdout)
    expected = {'status':None,'mongo':False,'size':None} if absent else {'status':'ready','mongo':True,'size':doc['size_bytes']}
    check(data == expected, ('Absent from' if absent else 'Present in') + ' all 3 stores: ' + doc['id'])
def save(state):
    STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
def verify_survivor(state):
    doc = state['survivor']
    status, page, _ = request('/ui/documents/' + doc['id'])
    check(status == 200 and b'day3.txt' in page, 'Document HTML')
    current = get_json('/documents/' + doc['id'])
    check(current['tags'] == ['day3','cloud'] and current['custom_metadata'] == {'checkpoint':'day3'}, 'Aggregated metadata')
    status, data, headers = request('/documents/' + doc['id'] + '/download')
    check(status == 200 and hashlib.sha256(data).hexdigest() == doc['sha256'], 'Download SHA256')
    check('day3.txt' in headers.get('Content-Disposition',''), 'Download filename')
    (OUTPUT / 'download.txt').write_bytes(data)
    storage(doc)
def before():
    check(get_json('/health/ready')['status'] == 'ready', 'Readiness')
    status, body, _ = request('/')
    check(status == 200 and b'Research Document Workspace' in body, 'Home HTML')
    check(request('/static/style.css')[0] == 200, 'Static CSS')
    check(form('/ui/projects', {'name':'   '})[0] == 422, 'Empty project rejected')
    check(request('/ui/documents/not-a-uuid')[0] == 422, 'Invalid UUID HTML')
    check(request('/ui/documents/' + str(uuid4()))[0] == 404, 'Unknown document HTML')
    status, _, headers = form('/ui/projects', {'name':'Day 3 ' + uuid4().hex[:8], 'description':'UI integration fixture'})
    check(status == 303, 'Create project UI')
    pid = headers['Location'].rsplit('/',1)[-1]
    state = {'project_id':pid, 'deleted':[]}
    save(state)
    check(request('/ui/projects/' + pid)[0] == 200, 'Project HTML')
    state['survivor'] = upload(pid, b'Day 3 survivor\n' + uuid4().hex.encode())
    save(state)
    target = upload(pid, b'Day 3 delete this test fixture\n')
    check(request('/ui/documents/' + target['id'] + '/delete')[0] == 200, 'Deletion confirmation page')
    check(form('/ui/documents/' + target['id'] + '/delete', {})[0] == 422, 'Deletion requires confirmation')
    check(form('/ui/documents/' + target['id'] + '/delete', {'confirm':'delete'})[0] == 303, 'Delete UI')
    check(request('/documents/' + target['id'])[0] == 404, '404 after delete')
    check(request('/documents/' + target['id'], 'DELETE')[0] == 200, 'Repeated DELETE is idempotent')
    storage(target, True)
    state['deleted'].append(target)
    state['failure_target'] = upload(pid, b'Day 3 retry deletion fixture\n')
    save(state)
    verify_survivor(state)
def failure():
    state = json.loads(STATE.read_text(encoding='utf-8'))
    check(request('/health/live')[0] == 200, 'Live during Mongo outage')
    check(request('/health/ready')[0] == 503, 'Not ready during Mongo outage')
    doc = state['failure_target']
    check(request('/documents/' + doc['id'], 'DELETE')[0] == 503, 'Partial deletion reports failure')
    rows = get_json('/projects/' + state['project_id'] + '/documents?limit=100')
    check(any(d['id'] == doc['id'] and d['status'] == 'deleting' for d in rows), 'Durable deleting status')
    check(request('/documents/' + doc['id'] + '/download')[0] == 409, 'Partial deletion blocks download')
    status, page, _ = request('/ui/documents/' + state['survivor']['id'])
    check(status == 503 and b'<!doctype html>' in page and b'Traceback' not in page, 'Storage error HTML')
def recovery():
    state = json.loads(STATE.read_text(encoding='utf-8'))
    check(get_json('/health/ready')['status'] == 'ready', 'Recovery readiness')
    doc = state['failure_target']
    check(request('/documents/' + doc['id'], 'DELETE')[0] == 200, 'Retry deletion after recovery')
    storage(doc, True)
    if not any(d['id'] == doc['id'] for d in state['deleted']):
        state['deleted'].append(doc)
    save(state)
    verify_survivor(state)
def after():
    state = json.loads(STATE.read_text(encoding='utf-8'))
    check(get_json('/health/ready')['status'] == 'ready', 'Readiness after recreation')
    check(get_json('/projects/' + state['project_id'])['id'] == state['project_id'], 'Same project persists')
    check(request('/ui/projects/' + state['project_id'])[0] == 200, 'Project HTML after recreation')
    verify_survivor(state)
    for doc in state['deleted']:
        check(request('/documents/' + doc['id'])[0] == 404, 'Deleted document stays absent')
        storage(doc, True)
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['before','failure','recovery','after'])
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    code = 0
    try:
        login(BASE, opener=OPENER)
        check(True, 'Admin login')
        globals()[args.phase]()
        RESULTS.append('DAY 3 ' + args.phase.upper() + ': PASS')
    except Exception as exc:
        code = 1
        RESULTS.append('DAY 3 ' + args.phase.upper() + ': FAIL - ' + str(exc))
    finally:
        (OUTPUT / (args.phase + '-result.txt')).write_text('\n'.join(RESULTS)+'\n', encoding='utf-8')
    print(RESULTS[-1])
    sys.exit(code)
