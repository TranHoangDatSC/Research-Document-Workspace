"""Run on Windows with Python 3.11+, from the project root, with the stack
healthy. Standard library only. No secrets needed: it only sends wrong
passwords, and reads REDIS_PASSWORD inside the container, never here.

Checks the scenarios of report table 4.2 against the real stack:
redis answers PING; the 21st failed login gets 429; the count survives a
`web` restart (it lives in Redis); with Redis stopped, logins still get an
answer (no 500), /health/ready stays 200 with redis "down", and the web log
shows the fallback. Restarts Redis and clears the test's counters at the end.

Output: artifacts/redis/redis-result.txt
"""
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

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
        clear_counters()

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
        require(status in (401, 429), 'login answered while Redis is stopped', f'HTTP {status}')
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
