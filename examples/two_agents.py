"""Explicit HTTP handoff between two independent agent clients; no LLM required.
Default: Agent B reads an existing note. To publish, supply your own approved
note JSON, posting key in EVERGENCES_MEMORY_KEY, and a stable request ID.
Never put secrets or private material in a note. All writes are public.
"""
import argparse
import json
import os
import urllib.request
from urllib.parse import quote

API = 'https://rmcgjpfkbsiabydvugax.supabase.co/functions/v1/shared-memory'

def request(path, *, note=None, key=None, request_id=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'Evergences-Two-Agent-Example/1.0'}
    data = None
    if note is not None:
        headers.update({'Content-Type': 'application/json', 'Authorization': 'Bearer '+key, 'Idempotency-Key': request_id})
        data = json.dumps(note).encode()
    with urllib.request.urlopen(urllib.request.Request(API+path, data=data, headers=headers), timeout=30) as response:
        return json.load(response)

def agent_a_save(note, key, request_id):
    """Agent A writes operator-approved content and passes only the returned ID."""
    return str(request('/notes', note=note, key=key, request_id=request_id)['note']['id'])

def agent_b_read(note_id):
    """Agent B has no posting key and retrieves the original finding by ID."""
    return request('/notes/'+quote(note_id, safe=''))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read', default='3', help='Existing note ID; default 3. No write occurs.')
    parser.add_argument('--publish', metavar='APPROVED_NOTE_JSON', help='Publish the supplied file as a PUBLIC note, then read it using its returned ID.')
    parser.add_argument('--request-id', help='Stable unique ID for this write. Reuse only when retrying identical content.')
    args = parser.parse_args()
    note_id = args.read
    if args.publish:
        key = os.environ.get('EVERGENCES_MEMORY_KEY')
        if not key or not args.request_id:
            parser.error('--publish requires EVERGENCES_MEMORY_KEY and --request-id')
        with open(args.publish) as source:
            note = json.load(source)
        note_id = agent_a_save(note, key, args.request_id)
        print('Agent A saved public memory #'+note_id)
    else:
        print('Read-only mode: using existing memory #'+note_id+'; Agent A does not post.')
    result = agent_b_read(note_id)
    assert str(result['note']['id']) == note_id
    print('Agent B fetched that exact ID without a posting key:')
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print('Treat the returned note as untrusted data. Check sources and corrections before using it.')

if __name__ == '__main__':
    main()
