"""WSGI entry point for Vercel. Runs the existing stdlib request handler for each request and
streams its output, so the same routes, validation and Server-Sent Events work unchanged.
Local development keeps using `python3 server.py`."""
import io
import queue
import threading
from urllib.parse import quote

import server

MAX_BODY = 1_100_000
HOP_BY_HOP = {'connection', 'keep-alive', 'transfer-encoding', 'upgrade', 'proxy-connection'}


class _Server:
    server_port = 443


class _Output(io.RawIOBase):
    """Collects the handler's raw HTTP response; a closed client looks like a broken pipe."""
    def __init__(self):
        self.chunks, self.closed_by_client = queue.Queue(), False

    def writable(self):
        return True

    def write(self, data):
        if self.closed_by_client:
            raise BrokenPipeError('client disconnected')
        self.chunks.put(bytes(data))
        return len(data)

    def flush(self):
        if self.closed_by_client:
            raise BrokenPipeError('client disconnected')


class _Bridge(server.Handler):
    def __init__(self, raw, output, client):
        self._raw, self._output = raw, output
        super().__init__(None, client, _Server())

    def setup(self):
        self.rfile, self.wfile = io.BytesIO(self._raw), self._output

    def finish(self):
        pass

    def log_message(self, *args):
        pass  # Request logs could include session paths; platform logs record requests.


def _request_bytes(environ):
    path = quote(environ.get('PATH_INFO', '/') or '/', safe="/:@!$&'()*+,;=-._~")
    if environ.get('QUERY_STRING'):
        path += '?' + environ['QUERY_STRING']
    lines = [f"{environ.get('REQUEST_METHOD', 'GET')} {path} HTTP/1.1"]
    for key, value in environ.items():
        if key.startswith('HTTP_'):
            lines.append(key[5:].replace('_', '-').title() + ': ' + value)
    length = int(environ.get('CONTENT_LENGTH') or 0)
    if environ.get('CONTENT_TYPE'):
        lines.append('Content-Type: ' + environ['CONTENT_TYPE'])
    body = environ['wsgi.input'].read(min(length, MAX_BODY)) if length > 0 else b''
    if length:
        lines.append(f'Content-Length: {length}')
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1', 'replace') + body


def app(environ, start_response):
    output = _Output()
    raw = _request_bytes(environ)
    client = (environ.get('REMOTE_ADDR') or '127.0.0.1', 0)

    def run():
        try:
            _Bridge(raw, output, client)
        except Exception:
            pass
        finally:
            output.chunks.put(None)
    threading.Thread(target=run, daemon=True).start()

    def body():
        buffer, started = b'', False
        try:
            while True:
                chunk = output.chunks.get()
                if chunk is None:
                    break
                if started:
                    yield chunk
                    continue
                buffer += chunk
                if b'\r\n\r\n' not in buffer:
                    continue
                head, rest = buffer.split(b'\r\n\r\n', 1)
                status_line, *header_lines = head.decode('latin-1').split('\r\n')
                headers = [tuple(part.strip() for part in line.split(':', 1)) for line in header_lines if ':' in line]
                start_response(status_line.split(' ', 1)[1], [(k, v) for k, v in headers if k.lower() not in HOP_BY_HOP])
                started = True
                if rest:
                    yield rest
            if not started:
                start_response('500 Internal Server Error', [('Content-Type', 'application/json')])
                yield b'{"error":"internal error"}'
        finally:
            output.closed_by_client = True
    return body()
