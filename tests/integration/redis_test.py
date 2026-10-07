"""Run on Windows with Python 3.11+, from the project root, with the stack
healthy. Standard library only. Signs in with ADMIN_USERNAME/ADMIN_PASSWORD
from .env (like the other integration tests); REDIS_PASSWORD is only used
inside the container.

Checks the scenarios of report table 4.2 against the real stack:
redis answers PING; a signed-in request caches the session check in Redis
(TTL <= 30 s) and "log out everywhere" deletes it; `web` runs several uvicorn
workers and a setting saved on one is seen by all (pub/sub), then restored to
its previous value; the `worker` service runs a job queued from `web`
(builtins.len("abc"), so no Gemini quota is spent); the 21st failed login gets
429; the count survives a `web` restart (it lives in Redis); with Redis
stopped, logins still get an answer (no 500), /health/ready stays 200 with
redis "down", and the web log shows the fallback. Restarts Redis and clears
the test's counters at the end.

Side effect: the admin account is logged out on every device.

Output: artifacts/redis/redis-result.txt
"""
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from _auth_helper import build_cookie_opener, login

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'artifacts' / 'redis'
BASE = 'http://127.0.0.1:8001'
USERNAME = 'redis-test-no-such-user'
LOGIN_LIMIT = 20
results = []


def require(condition, name, detail=''):
    line = f"{name}: {'PASS' if condition else 'FAIL'}{' (' + detail + ')' if detail else ''}"
    results.append(line)
    print(line, flush=True)
    if not condition:
        raise RuntimeError(line)


def compose(*args, check=True):
    return subprocess.run(['docker', 'compose', *args], cwd=ROOT, capture_output=True, text=True, check=check)


def redis_cli(*args):
    # REDISCLI_AUTH is set inside the container (docker-compose.yaml).
    return compose('exec', '-T', 'redis', 'redis-cli', *args).stdout.strip()


def http(path, method='GET', form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {'Content-Type': 'application/x-www-form-urlencoded'} if form is not None else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wrong_login():
    status, _ = http('/login', 'POST', {'username': USERNAME, 'password': 'wrong-password'})
    return status


def clear_counters():
    keys = redis_cli('--scan', '--pattern', 'rl:login:*').split()
    if keys:
        redis_cli('del', *keys)


def check_session_cache():
    opener = build_cookie_opener()
    login(BASE, opener)
    with opener.open(BASE + '/account', timeout=20) as response:
        require(response.status == 200, 'signed-in page loads')
    keys = redis_cli('--scan', '--pattern', 'session:*').split()
    require(bool(keys), 'session check cached in Redis', ' '.join(keys))
    ttls = [int(redis_cli('ttl', key)) for key in keys]
    require(all(0 < ttl <= 30 for ttl in ttls), 'session cache TTL <= 30 s', str(ttls))

    req = urllib.request.Request(BASE + '/account/logout-everywhere', data=b'', method='POST')
    try:
        opener.open(req, timeout=20).close()
    except urllib.error.HTTPError:
        pass  # the 303 to /login may surface as an error, the POST already ran
    remaining = [key for key in keys if redis_cli('exists', key) == '1']
    require(not remaining, '"log out everywhere" deletes the cached session', ' '.join(remaining))


WORKER_COUNT = ("import pathlib; print(sum(1 for p in pathlib.Path('/proc').glob('[0-9]*/cmdline') "
                "if b'spawn_main' in p.read_bytes()))")
SYNC_KEY, SYNC_VALUE = 'LLM_TIMEOUT_SECONDS', '77'


def saved_setting(key):
    sql = f"select value from app_settings where key = '{key}'"
    out = compose('exec', '-T', 'postgres', 'sh', '-c', f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "{sql}"').stdout
    return out.strip()


def save_setting(opener, key, value):
    data = urllib.parse.urlencode({'key': key, 'value': value}).encode()
    req = urllib.request.Request(BASE + '/admin/settings', data=data, method='POST',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    opener.open(req, timeout=20).close()


def check_settings_sync():
    workers = int(compose('exec', '-T', 'web', 'python', '-c', WORKER_COUNT).stdout.strip() or 0)
    require(workers >= 2, 'web runs several uvicorn workers', f'{workers} workers')

    opener = build_cookie_opener()
    login(BASE, opener)
    previous = saved_setting(SYNC_KEY)
    try:
        save_setting(opener, SYNC_KEY, SYNC_VALUE)
        time.sleep(1)  # pub/sub delivery to the other workers
        expected = f'value="{SYNC_VALUE}"'
        pages = []
        for _ in range(30):  # new connection each time: spread over the workers
            with opener.open(BASE + '/admin/settings', timeout=20) as response:
                pages.append(expected in response.read().decode('utf-8'))
        require(all(pages), 'setting saved once is seen by every worker', f'{sum(pages)}/30 pages')
    finally:
        save_setting(opener, SYNC_KEY, previous)
    results.append(f'{SYNC_KEY} restored to previous value: {previous or "(.env)"}')
    print(results[-1], flush=True)


QUEUE_PROBE = """
import time
from rq import Queue
from app import jobs
from app.storage import redis_client
job = Queue(jobs.QUEUE, connection=redis_client()).enqueue('builtins.len', 'abc')
for _ in range(60):
    if job.get_status(refresh=True) in ('finished', 'failed'):
        break
    time.sleep(0.5)
status = job.get_status(refresh=True)
print(getattr(status, 'value', status), job.return_value())  # JobStatus enum -> 'finished'
"""


def check_worker():
    status = compose('ps', '--format', '{{.Status}}', 'worker').stdout.strip()
    require('healthy' in status, 'worker service healthy', status)
    out = compose('exec', '-T', 'web', 'python', '-c', QUEUE_PROBE).stdout.strip()
    require(out == 'finished 3', 'job queued by web is run by the worker', out)


def wait_ready():
    for _ in range(60):
        try:
            status, _ = http('/health/ready')
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError('web did not become ready')


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    try:
        require(redis_cli('ping') == 'PONG', 'redis-cli ping returns PONG')
        clear_counters()  # leftovers of an interrupted run would block the sign-in
        check_session_cache()
        check_settings_sync()
        check_worker()
        clear_counters()  # the real sign-ins above count as attempts

        statuses = [wrong_login() for _ in range(LOGIN_LIMIT)]
        require(all(s == 401 for s in statuses), f'{LOGIN_LIMIT} wrong logins answered 401')
        require(wrong_login() == 429, f'login #{LOGIN_LIMIT + 1} answered 429')
        keys = redis_cli('--scan', '--pattern', 'rl:login:*')
        require(bool(keys), 'counter stored in Redis', keys)

        compose('restart', 'web')
        wait_ready()
        require(wrong_login() == 429, 'still 429 after restarting web')

        compose('stop', 'redis')
        status = wrong_login()
        require(status in (401, 429), 'wrong login answered while Redis is stopped', f'HTTP {status}')
        opener = build_cookie_opener()
        login(BASE, opener)  # raises unless 200/303
        with opener.open(BASE + '/account', timeout=20) as response:
            require(response.status == 200, 'correct login works while Redis is stopped')
        status, body = http('/health/ready')
        services = json.loads(body).get('services', {})
        require(status == 200 and services.get('redis') == 'down', '/health/ready 200 with redis down', json.dumps(services))
        logs = compose('logs', '--since', '2m', 'web').stdout
        require('ratelimit_redis_unavailable' in logs, 'web log shows the in-memory fallback')
    finally:
        compose('start', 'redis', check=False)
        compose('up', '-d', '--wait', 'redis', check=False)
        try:
            clear_counters()
        except subprocess.CalledProcessError:
            print('WARNING: could not clear rl:login:* keys; they expire within 5 minutes')

    stats = subprocess.run(
        ['docker', 'stats', '--no-stream', '--format', '{{.Name}} {{.MemUsage}}'],
        capture_output=True, text=True, check=True,
    ).stdout
    redis_mem = next((line for line in stats.splitlines() if 'redis' in line), 'not found')
    results.append(f'docker stats redis: {redis_mem}')
    print(results[-1])

    (OUTPUT / 'redis-result.txt').write_text('\n'.join(results) + '\n', encoding='utf-8')
    (OUTPUT / 'compose-ps.txt').write_text(compose('ps').stdout, encoding='utf-8')
    print(f'REDIS CHECK: PASS -> {OUTPUT}')


if __name__ == '__main__':
    main()
