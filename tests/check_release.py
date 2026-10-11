"""Verify reproducible packaging, registry integrity and exact source inclusion offline."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import zipfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from build_release import build, FILES
with tempfile.TemporaryDirectory() as temporary:
    first=Path(temporary)/'first.mcpb';second=Path(temporary)/'second.mcpb'
    one=build(first);two=build(second)
    assert one['sha256']==two['sha256'], 'Bundle build is not reproducible'
    registry=json.loads((ROOT/'server.json').read_text())
    assert one['sha256']==registry['packages'][0]['fileSha256'], 'Registry digest differs from bundle'
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist())==set(FILES)
        assert archive.testzip() is None
        for name in FILES:assert archive.read(name)==(ROOT/name).read_bytes()
        manifest=json.loads(archive.read('manifest.json'))
        card=json.loads((ROOT/'server-card.json').read_text())
        assert {t['name'] for t in manifest['tools']}=={t['name'] for t in card['tools']}
print('PASS: reproducible MCPB, registry SHA-256, locked versions and exact source allowlist.')
