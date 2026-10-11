"""Bounded offline fixture measurements; never a production capacity benchmark."""
from test_recovery import RecoveryCache, IDENTITY, event, page
import json
import tempfile
import time
from pathlib import Path
with tempfile.TemporaryDirectory() as d:
    c=RecoveryCache(Path(d)/'fixture.sqlite',**IDENTITY)
    start=time.perf_counter()
    for first in range(1,1001,50):
        c.apply_page(page([event(n) for n in range(first,first+50)],watermark=1000),**IDENTITY)
    elapsed=time.perf_counter()-start
    cp=c.checkpoint(); c.close()
    c=RecoveryCache(Path(d)/'fixture.sqlite',**IDENTITY);start=time.perf_counter()
    context=c.recover('fixture-workflow',checkpoint=cp,current_workspace_id=IDENTITY['workspace_id'],current_scope='read',current_source_manifest_sha256='a'*64)
    recovery=time.perf_counter()-start
    result=dict(evidence='synthetic_offline_mac',events=1000,pages=20,append_seconds=round(elapsed,6),fresh_cache_recovery_seconds=round(recovery,6),database_bytes=(Path(d)/'fixture.sqlite').stat().st_size,last_durable_sequence=cp['last_durable_sequence'],returned_records=len(context['records']),truncated=context['truncated'],action_authorized=context['action_authorized'])
    print(json.dumps(result,indent=2));c.close()
