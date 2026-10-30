"""Collect current local evidence without changing business code or starting services."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[2]
review = root / 'docs/review'
suite = ET.parse(review / 'next-pytest.xml').getroot().find('testsuite')
cases = suite.findall('testcase')
timing = next(p.attrib['value'] for c in cases if '100000_metadata' in c.attrib['name']
              for p in c.findall('./properties/property') if p.attrib['name'] == 'sqlite_metadata_seconds')
output = root / 'src/WindowsAIPhotos/bin/x64/Release/net10.0-windows10.0.19041.0/engine'
shipped = []
for source in sorted((root / 'engine/ai_photos').rglob('*')):
    if not source.is_file() or ('__pycache__' in source.parts) or source.suffix not in {'.py', '.html', '.js', '.css'}:
        continue
    relative = source.relative_to(root / 'engine')
    assert source.read_bytes() == (output / relative).read_bytes(), relative
    shipped.append(relative.as_posix())
syntax = json.loads((review / 'next-swift-syntax.json').read_text())
assert not any(item['has_error'] for item in syntax['files'])
assert '0 Warning(s)' in (review / 'next-dotnet-build.log').read_text(encoding='utf-8-sig')
assert '0 Error(s)' in (review / 'next-dotnet-build.log').read_text(encoding='utf-8-sig')
result = {
    'pytest_passed': len(cases) - int(suite.attrib['skipped']) - int(suite.attrib['failures']) - int(suite.attrib['errors']),
    'pytest_skipped': int(suite.attrib['skipped']), 'pytest_failed': int(suite.attrib['failures']),
    'pytest_errors': int(suite.attrib['errors']), 'pytest_seconds': float(suite.attrib['time']),
    'sqlite_100000_metadata_two_snapshots_seconds': float(timing),
    'metadata_scope': 'In-memory SQLite using SQL extracted from Swift; not PhotoKit or native Swift performance',
    'swift_grammar_files': len(syntax['files']), 'swift_xcode_native_tests': 'Not Verified',
    'windows_build_warnings': 0, 'windows_build_errors': 0,
    'output_files_byte_verified': shipped,
    'evidence_sha256': {name: hashlib.sha256((review / name).read_bytes()).hexdigest() for name in [
        'next-pytest.xml', 'next-pytest.log', 'next-dotnet-build.log', 'next-output-smoke.log',
        'next-swift-syntax.json', 'next-gui-lifecycle.json', 'next-static-check.log']}
}
(review / 'next-validation.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, indent=2))
assert result['pytest_failed'] == result['pytest_errors'] == 0
