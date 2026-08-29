#!/usr/bin/env python3
from pathlib import Path
import hashlib, shutil, sys
root=Path(__file__).resolve().parent
target=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else root/'MODIFIED_FILE'
if not target.exists(): raise SystemExit('missing rollback input')
probe=root/'rollback-test-copy'; shutil.copy2(target,probe)
try:
    before=hashlib.sha256(probe.read_bytes()).hexdigest()
    baseline=(root/'MODIFIED_FILE').read_bytes(); probe.write_bytes(baseline)
    restored=hashlib.sha256(probe.read_bytes()).hexdigest(); expected=hashlib.sha256(baseline).hexdigest()
    print(f'ROLLBACK_TARGET={probe}')
    print(f'BEFORE_SHA256={before}')
    print(f'RESTORED_SHA256={restored}')
    print(f'EXPECTED_SHA256={expected}')
    print('RESTORED_STATUS=PASS' if restored==expected else 'RESTORED_STATUS=FAIL')
    if restored!=expected: raise SystemExit(1)
finally:
    probe.unlink(missing_ok=True)
