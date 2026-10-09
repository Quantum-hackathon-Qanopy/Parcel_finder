"""Serve the dashboard locally, with an "Add project" button that runs the pipeline.

    python src/dashboard_server.py          # then open http://127.0.0.1:8765

A page opened straight from disk cannot run anything, so this small server sits behind the
dashboard. The "Add project" form on the page sends the boundary file and the project's facts
here, and the server runs `add_project.py` for it (the same checks and the same pipeline as on
the command line), streaming its output back to the page. When it finishes, reload the page and
the new project has its own tab.

The server only listens on this computer (127.0.0.1) and only accepts a request that carries the
random token it put into the page. Publishing to the NFT uses the keys in the .env file, which
the browser never sees. One project runs at a time, because the Earth Engine work is heavy.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from config import DASHBOARD_HTML, OUTPUT_ROOT, ROOT, read_registry

UPLOAD_DIR = OUTPUT_ROOT / '_uploads'
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_BODY = 80 * 1024 * 1024          # a boundary file can be tens of megabytes
LOG_TAIL = 40_000                    # characters of a job's output sent to the page
TOKEN = secrets.token_urlsafe(24)
JOBS = {}
LOCK = threading.Lock()


# ---------------------------------------------------------------------------------- the jobs
def safe_name(name):
    """A plain file name for an uploaded boundary: no folders, only ordinary characters."""
    base = re.sub(r'[^A-Za-z0-9._ -]', '_', Path(str(name)).name).strip(' .') or 'boundary'
    return base if base.lower().endswith('.kml') else base + '.kml'


def number(value, label, optional=False, positive=True):
    if value in (None, ''):
        if optional:
            return None
        raise ValueError(f'{label} is needed')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label} must be a number')
    if positive and result <= 0:
        raise ValueError(f'{label} must be above zero')
    return result


def build_command(payload):
    """Validate the form and return (add_project.py arguments, the slug). Raises ValueError."""
    slug = str(payload.get('slug', '')).strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', slug):
        raise ValueError('The short name may only use letters, digits, - and _')
    if slug in read_registry() and not payload.get('replace'):
        raise ValueError(f"A project called '{slug}' already exists")
    name = str(payload.get('name', '')).strip()
    if not name:
        raise ValueError('The project name is needed')
    dates = {}
    for key, label in (('start', 'The start date'), ('end', 'The end date')):
        try:
            dates[key] = date.fromisoformat(str(payload.get(key, '')))
        except ValueError:
            raise ValueError(f'{label} is needed (YYYY-MM-DD)')
    if dates['end'] <= dates['start']:
        raise ValueError('The end date must be after the start date')
    kml_text = payload.get('kml_text')
    if not isinstance(kml_text, str) or '<' not in kml_text:
        raise ValueError('Choose a boundary KML file')
    path = UPLOAD_DIR / safe_name(payload.get('kml_name'))
    path.write_text(kml_text, encoding='utf-8')

    args = [slug, '--name', name, '--kml', str(path), '--start', dates['start'].isoformat(),
            '--end', dates['end'].isoformat(), '--claim', str(number(payload.get('claim'), 'The annual claim'))]
    registered = number(payload.get('registered_ha'), 'The registered area', optional=True)
    if registered:
        args += ['--registered-ha', str(registered)]
    mode = payload.get('density', 'none')
    if mode == 'assumption':
        args += ['--carbon-tc-per-ha', str(number(payload.get('carbon_tc_per_ha'), 'The carbon density'))]
    elif mode == 'projection':
        args += ['--projected-ha', str(number(payload.get('projected_ha'), 'The projected avoided hectares')),
                 '--projected-tco2e', str(number(payload.get('projected_tco2e'), 'The projected avoided tCO2e'))]
    elif mode != 'none':
        raise ValueError('Unknown carbon density option')
    check_only = bool(payload.get('check_only'))
    if check_only:
        args.append('--check-only')
    else:
        args.append('--run')
        if payload.get('publish') and mode != 'none':
            args.append('--publish')
    if payload.get('allow_problems') or payload.get('replace'):
        args.append('--force')
    return args, slug


def start_job(payload):
    """Run add_project.py for the form's values in the background. Returns the job id."""
    with LOCK:
        if any(job['proc'].poll() is None for job in JOBS.values()):
            raise RuntimeError('Another project is still running. Wait for it to finish.')
        args, slug = build_command(payload)
        job_id = uuid.uuid4().hex[:10]
        log_path = UPLOAD_DIR / f'job_{job_id}.log'
        log = open(log_path, 'w', encoding='utf-8')
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        proc = subprocess.Popen([sys.executable, '-u', str(ROOT / 'src' / 'add_project.py'), *args],
                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env)
        JOBS[job_id] = {'proc': proc, 'log_path': log_path, 'slug': slug, 'started': time.time(),
                        'check_only': '--check-only' in args, 'publish': '--publish' in args}
        return job_id


def job_status(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return None
    code = job['proc'].poll()
    text = job['log_path'].read_text(encoding='utf-8', errors='replace') if job['log_path'].exists() else ''
    return {'id': job_id, 'slug': job['slug'], 'state': 'running' if code is None else
            ('finished' if code == 0 else 'failed'), 'exit': code, 'check_only': job['check_only'],
            'publish': job['publish'], 'seconds': int(time.time() - job['started']), 'log': text[-LOG_TAIL:]}


# ---------------------------------------------------------------------------------- the server
class Handler(BaseHTTPRequestHandler):
    server_version = 'QanopyDashboard'

    def log_message(self, *args):                          # keep the terminal quiet
        pass

    def send(self, code, body, content_type):
        data = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj), 'application/json; charset=utf-8')

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            if not DASHBOARD_HTML.exists():
                return self.send(200, '<h1>No dashboard yet</h1><p>Run <code>python src/monitor.py</code> '
                                      'for a project first.</p>', 'text/html; charset=utf-8')
            html = DASHBOARD_HTML.read_text(encoding='utf-8')
            script = f'<script>window.QANOPY_SERVER = {{token: {json.dumps(TOKEN)}}};</script>'
            return self.send(200, html.replace('<head>', '<head>' + script, 1), 'text/html; charset=utf-8')
        if path == '/api/projects':
            return self.send_json(200, [{'slug': slug, 'name': entry['name'],
                                         'has_results': (OUTPUT_ROOT / slug / 'dashboard_payload.json').exists()}
                                        for slug, entry in read_registry().items()])
        if path.startswith('/api/jobs/'):
            status = job_status(path.rsplit('/', 1)[-1])
            return self.send_json(200 if status else 404, status or {'error': 'unknown job'})
        self.send(404, 'Not found', 'text/plain')

    def do_POST(self):
        if urlparse(self.path).path != '/api/add':
            return self.send(404, 'Not found', 'text/plain')
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 or length > MAX_BODY:
            self.close_connection = True
            return self.send_json(413, {'error': 'The upload is empty or too large.'})
        raw = self.rfile.read(length)             # always read the body, or the reply cannot be delivered
        if self.headers.get('X-Token') != TOKEN:
            return self.send_json(403, {'error': 'Missing or wrong token. Reload the page.'})
        try:
            self.send_json(200, {'job': start_job(json.loads(raw.decode('utf-8')))})
        except ValueError as err:
            self.send_json(400, {'error': str(err)})
        except RuntimeError as err:
            self.send_json(409, {'error': str(err)})


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-open', action='store_true', help='do not open the browser')
    args = parser.parse_args()
    for port in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f'No free port from {args.port} to {args.port + 9}.')
    url = f'http://127.0.0.1:{port}'
    print(f'Dashboard at {url}  (press Ctrl+C to stop)')
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
