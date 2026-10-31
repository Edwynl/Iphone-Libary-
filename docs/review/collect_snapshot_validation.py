"""Collect local evidence for the PR #3 blocker repair; no services are started."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[2]
review = root / 'docs/review'
suite = ET.parse(review / 'snapshot-pytest.xml').getroot().find('testsuite')
cases = suite.findall('testcase')
output = root / 'src/WindowsAIPhotos/bin/x64/Release/net10.0-windows10.0.19041.0/engine'
shipped = []
for path in sorted((root / 'engine/ai_photos').rglob('*')):
    if not path.is_file() or '__pycache__' in path.parts or path.suffix not in {'.py', '.html', '.js', '.css'}:
        continue
    relative = path.relative_to(root / 'engine')
    assert path.read_bytes() == (output / relative).read_bytes(), relative
    shipped.append(relative.as_posix())
calls = []
for path in (root / 'ios/PhotosCompanion/Sources').glob('*.swift'):
    for line, text in enumerate(path.read_text().splitlines(), 1):
        if 'BackgroundScheduler.schedule(' in text:
            calls.append({'path': path.relative_to(root).as_posix(), 'line': line, 'source': text.strip()})
assert calls and all('try BackgroundScheduler.schedule(' in call['source'] for call in calls)
assert 'sync.scheduleBackground()' in (root / 'ios/PhotosCompanion/Sources/PhotosCompanionApp.swift').read_text()
syntax = json.loads((review / 'snapshot-swift-syntax.json').read_text())
assert not any(item['has_error'] for item in syntax['files'])
build = (review / 'snapshot-dotnet-build.log').read_text(encoding='utf-8-sig')
assert '0 Warning(s)' in build and '0 Error(s)' in build
interface = json.loads((review / 'snapshot-interface-smoke.log').read_text(encoding='utf-8-sig'))
assert all(interface.values())
result = {
    'pytest_passed': len(cases) - int(suite.attrib['skipped']) - int(suite.attrib['failures']) - int(suite.attrib['errors']),
    'pytest_skipped': int(suite.attrib['skipped']), 'pytest_failures': int(suite.attrib['failures']),
    'pytest_errors': int(suite.attrib['errors']), 'pytest_seconds': float(suite.attrib['time']),
    'new_snapshot_tests_passed': sum('test_backup_snapshots' in c.attrib['classname'] for c in cases),
    'new_snapshot_junction_tests_passed': sum('snapshot_storage_junction' in c.attrib['name'] for c in cases),
    'shipped_interface_checks': interface, 'shipped_files_byte_verified': shipped,
    'scheduler_direct_call_sites': calls, 'swift_grammar_files': len(syntax['files']),
    'swift_type_sdk_xcode_device_verification': 'Not Verified',
    'actual_power_loss_cross_volume_cloud_recovery': 'Not Verified',
    'evidence_working_tree_sha256': {name: hashlib.sha256((review / name).read_bytes()).hexdigest() for name in [
        'snapshot-pytest.xml', 'snapshot-pytest.log', 'snapshot-dotnet-build.log',
        'snapshot-interface-smoke.log', 'snapshot-output-smoke.log', 'snapshot-static-check.log',
        'snapshot-swift-syntax.json']}
}
(review / 'snapshot-validation.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, indent=2))
assert result['pytest_failures'] == result['pytest_errors'] == 0
