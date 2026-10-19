"""Build a reproducible MCP bundle using the existing release's explicit file set.

No credentials, environment files, tests or local cache files enter the bundle.
Run with --update-registry to record its digest before committing release metadata.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ['LICENSE', 'README.md', 'manifest.json', 'pyproject.toml', 'uv.lock',
         'src/shared_memory_mcp/__init__.py', 'src/shared_memory_mcp/server.py',
         'src/shared_memory_mcp/recovery.py', 'src/shared_memory_mcp/schema2_reference.py',
         'src/shared_memory_mcp/runtime_state.py',
         'docs/workflow-integration.md']


def build(output):
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    registry = json.loads((ROOT / 'server.json').read_text())
    version = re.search(r'^version = "([^"]+)"$', (ROOT / 'pyproject.toml').read_text(), re.M)[1]
    assert manifest['version'] == registry['version'] == version, 'Release versions disagree'
    assert f'name = "evergences-shared-memory-mcp"\nversion = "{version}"' in (ROOT / 'uv.lock').read_text(), 'Root lock version differs'
    assert registry['packages'][0]['identifier'].endswith(f'/v{version}/evergences-shared-memory.mcpb'), 'Release asset URL differs'
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 11, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (ROOT / name).read_bytes())
    return {'version': version, 'bundle': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'files': len(FILES)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=str(ROOT / 'dist/evergences-shared-memory.mcpb'))
    parser.add_argument('--update-registry', action='store_true')
    args = parser.parse_args()
    result = build(args.output)
    if args.update_registry:
        path = ROOT / 'server.json'
        registry = json.loads(path.read_text())
        registry['packages'][0]['fileSha256'] = result['sha256']
        path.write_text(json.dumps(registry, indent=2) + '\n')
    print(json.dumps(result))
