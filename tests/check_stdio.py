"""Exercise an actual stdio process with no API key and no tool execution."""
import json, os, subprocess, sys, threading

env = dict(os.environ)
env.pop("EVERGENCES_MEMORY_KEY", None)
p = subprocess.Popen(sys.argv[1:] or ["evergences-shared-memory"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
timer = threading.Timer(45, p.kill)
timer.start()
try:
    def send(value):
        p.stdin.write(json.dumps(value) + "\n")
        p.stdin.flush()
    def response(request_id):
        while True:
            line = p.stdout.readline()
            assert line, "Server stopped or timed out: " + p.stderr.read()[:1000]
            value = json.loads(line)
            if value.get("id") == request_id:
                assert "error" not in value, value
                return value["result"]
    send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"glama-readiness-check","version":"1.0"}}})
    assert "serverInfo" in response(1)
    send({"jsonrpc":"2.0","method":"notifications/initialized"})
    send({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
    names = {tool["name"] for tool in response(2)["tools"]}
    assert names == {"search_notes", "read_note", "post_note"}, names
    print("PASS: real stdio initialization and all three tools; no credentials or writes.")
finally:
    timer.cancel()
    p.terminate()
    try: p.wait(timeout=5)
    except subprocess.TimeoutExpired: p.kill(); p.wait()
