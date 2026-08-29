from pathlib import Path
import hashlib, json
from payment_link_extractor.gopay_pro_core import GOPAY_CORE_SOURCE_DIR

def test_imported_gopay_go_source_manifest_is_intact():
    manifest = json.loads((GOPAY_CORE_SOURCE_DIR / 'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    assert manifest['channel'] == 'gopay'
    assert manifest['tracked_file_count'] == 12
    for rel, expected in manifest['sha256'].items():
        assert hashlib.sha256((GOPAY_CORE_SOURCE_DIR / rel).read_bytes()).hexdigest() == expected

def test_gopay_core_is_not_paypal_adapter():
    text = (Path(__file__).resolve().parents[1] / 'payment_link_extractor/gopay_channel.py').read_text(encoding='utf-8')
    assert 'paypal_channel' not in text
    assert 'gopay_pro_core.core' in text
