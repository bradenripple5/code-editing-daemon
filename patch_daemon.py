"""Local multi-file patch worker and client. Python standard library only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen


PATTERN = re.compile(r'(REPLACE|INSERT_AFTER|INSERT_BEFORE|REMOVE)\s*\r?\n<<<\r?\n([\s\S]*?)(?:\r?\n===\r?\n([\s\S]*?))?\r?\n>>>')


def parse_directions(text):
    operations, end = [], 0
    for match in PATTERN.finditer(text):
        if text[end:match.start()].strip():
            raise ValueError('Unrecognized patch directions')
        kind, target, value = match.groups()
        if kind != 'REMOVE' and value is None:
            raise ValueError('Missing === separator')
        operations.append(dict(type=kind, target=target, value=value or ''))
        end = match.end()
    if not operations or text[end:].strip():
        raise ValueError('Invalid or empty patch directions')
    return operations


def execute_patch(source, operations):
    if not isinstance(operations, list) or not operations:
        raise ValueError('operations must be a nonempty list')
    for op in operations:
        kind, target, value = op['type'], op['target'], op.get('value', '')
        if not isinstance(target, str) or not target or not isinstance(value, str):
            raise ValueError('target must be nonempty text and value must be text')
        count = source.count(target)
        if count != 1:
            raise ValueError(f'{kind}: expected exactly one match, found {count}')
        if kind == 'REPLACE':
            replacement = value
        elif kind == 'REMOVE':
            replacement = ''
        elif kind == 'INSERT_BEFORE':
            replacement = value + target
        elif kind == 'INSERT_AFTER':
            replacement = target + value
        else:
            raise ValueError(f'Unknown operation: {kind}')
        source = source.replace(target, replacement, 1)
    return source


def digest(data):
    return hashlib.sha256(data).hexdigest()


def replace_bytes(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.patch-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Worker:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.lock = threading.Lock()

    def apply(self, request):
        with self.lock:
            return self._apply(request)

    def _apply(self, request):
        files = request.get('files')
        dry_run = request.get('dry_run', True)
        if not isinstance(dry_run, bool):
            raise ValueError('dry_run must be a boolean')
        if not isinstance(files, list) or not files:
            raise ValueError('files must be a nonempty list')
        staged, seen = [], set()
        for item in files:
            relative = Path(item['path'])
            if relative.is_absolute() or relative.drive or '..' in relative.parts or ':' in str(relative):
                raise ValueError('Expected a relative workspace path')
            if any(part.lower() in {'.git', '.codex', '.agents', '.aws', '.patch-daemon'} for part in relative.parts):
                raise ValueError('Protected path')
            path = (self.root / relative).resolve(strict=True)
            if not path.is_relative_to(self.root) or not path.is_file():
                raise ValueError('Path must be a file inside the workspace')
            if path in seen:
                raise ValueError('Duplicate file in batch')
            seen.add(path)
            before = path.read_bytes()
            if item.get('sha256') and item['sha256'] != digest(before):
                raise ValueError(f'Stale file: {relative}')
            operations = item.get('operations')
            if operations is None:
                operations = parse_directions(item['directions'])
            after = execute_patch(before.decode('utf-8'), operations).encode('utf-8')
            staged.append((path, before, after))
        # Validate the whole batch before touching any file.
        if not dry_run:
            written = []
            try:
                for path, before, after in staged:
                    if path.read_bytes() != before:
                        raise ValueError(f'File changed during batch: {path.name}')
                    if before != after:
                        replace_bytes(path, after)
                        written.append((path, before, after))
            except Exception as error:
                failures = []
                for path, before, after in reversed(written):
                    try:
                        if path.read_bytes() != after:
                            raise ValueError('File changed externally; rollback skipped')
                        replace_bytes(path, before)
                    except Exception as rollback_error:
                        failures.append(f'{path}: {rollback_error}')
                if failures:
                    raise RuntimeError(f'{error}; incomplete rollback: {failures}') from error
                raise
        return {'ok': True, 'dry_run': dry_run, 'files': [
            {'path': str(path.relative_to(self.root)), 'changed': before != after,
             'before_sha256': digest(before), 'after_sha256': digest(after)}
            for path, before, after in staged]}


def serve(root, state, port):
    worker = Worker(root)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get('Authorization') != f'Bearer {token}':
                self.reply(401, {'ok': False, 'error': 'Unauthorized'})
                return
            if self.path != '/apply':
                self.reply(404, {'ok': False, 'error': 'Unknown endpoint'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 10 * 1024 * 1024:
                    raise ValueError('Request must be between 1 byte and 10 MiB')
                self.connection.settimeout(30)
                result = worker.apply(json.loads(self.rfile.read(size)))
                self.reply(200, result)
            except Exception as error:
                self.reply(400, {'ok': False, 'error': str(error)})

        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    state = Path(state).resolve()
    state.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally replacing another daemon's credentials.
    with state.open('x', encoding='utf-8') as stream:
        json.dump({'url': f'http://127.0.0.1:{server.server_port}', 'token': token,
                   'pid': os.getpid(), 'root': str(worker.root)}, stream)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        state.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    daemon = sub.add_parser('serve')
    daemon.add_argument('--root', default='.')
    daemon.add_argument('--port', type=int, default=0)
    daemon.add_argument('--state', default='.patch-daemon/connection.json')
    client = sub.add_parser('submit')
    client.add_argument('request', help='JSON request file')
    client.add_argument('--state', default='.patch-daemon/connection.json')
    args = parser.parse_args()
    if args.command == 'serve':
        serve(args.root, args.state, args.port)
    else:
        from urllib.error import HTTPError
        connection = json.loads(Path(args.state).read_text(encoding='utf-8'))
        request = Request(connection['url'] + '/apply', data=Path(args.request).read_bytes(),
                          headers={'Authorization': 'Bearer ' + connection['token'],
                                   'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=120) as response:
                print(response.read().decode())
        except HTTPError as error:
            print(error.read().decode())
            raise SystemExit(1)


if __name__ == '__main__':
    main()
