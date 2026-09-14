"""Exercise the write/read handoff against a local HTTP fixture; no public writes."""
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location('example', Path('examples/two_agents.py'))
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)
received = {}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        assert self.path == '/notes'
        assert self.headers['Authorization'] == 'Bearer test-only-key'
        assert self.headers['Idempotency-Key'] == 'test-handoff'
        received.update(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.respond({'note': {**received, 'id': '17'}})
    def do_GET(self):
        assert self.path == '/notes/17'
        assert self.headers.get('Authorization') is None
        self.respond({'note': {**received, 'id': '17'}, 'replies': []})
    def respond(self, value):
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps(value).encode())
server = HTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
example.API = 'http://127.0.0.1:'+str(server.server_port)
try:
    note = {'title':'Local fixture','summary':'A local test','body':'Not published','kind':'finding','tags':[],'sources':['https://example.com']}
    pointer = example.agent_a_save(note, 'test-only-key', 'test-handoff')
    result = example.agent_b_read(pointer)
    assert pointer == '17' and result['note'] == {**note,'id':'17'}
    print('PASS: Agent A writes; Agent B retrieves the assigned ID with no posting credential. No public writes.')
finally:
    server.shutdown(); server.server_close()
