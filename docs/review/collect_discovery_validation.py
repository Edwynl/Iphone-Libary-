"""Collect discovery/progress evidence; no native/iOS success is inferred."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[2]
review = root / 'docs/review'
suite = ET.parse(review / 'discovery-pytest.xml').getroot().find('testsuite')
syntax = json.loads((review / 'discovery-swift-syntax.json').read_text())
assert not any(file['has_error'] for file in syntax['files'])
changed = subprocess.check_output(['git', 'diff', '--name-only', 'c62f97c', '--'], cwd=root, text=True).splitlines()
assert not any(name.startswith(('engine/', 'src/WindowsAIPhotos/')) for name in changed)
sources = sorted((root / 'ios').rglob('*.swift'))
native_cases = sum(len(re.findall(r'func test\w+', p.read_text())) for p in (root / 'ios/PhotosCompanion/QueueTests').glob('*.swift'))
result = {
    'reviewed_baseline': 'c62f97c098afe89fd9bd30579eef83ee8323c93d',
    'latest_independent_review_at_start': '3886da6e26e6da7a2b8806bd378828998ccbd560',
    'pytest_passed': int(suite.attrib['tests']) - int(suite.attrib['skipped']) - int(suite.attrib['failures']) - int(suite.attrib['errors']),
    'pytest_skipped': int(suite.attrib['skipped']), 'pytest_failures': int(suite.attrib['failures']),
    'pytest_errors': int(suite.attrib['errors']), 'pytest_seconds': float(suite.attrib['time']),
    'python_queue_sql_tests': sum('test_swift_queue_contract' in c.attrib['classname'] for c in suite.findall('testcase')),
    'swift_grammar_files': len(syntax['files']), 'prepared_native_xctests': native_cases,
    'native_xctests_executed': False, 'xcode_photokit_device_energy_validation': 'Not Verified',
    'windows_engine_and_winui_sources_changed': False, 'windows_build_repeated_this_delta': False,
    'cache_scope': 'Process-local completed discovery only; no persistent PhotoKit history token',
    'evidence_working_tree_sha256': {name: hashlib.sha256((review / name).read_bytes()).hexdigest()
        for name in ['discovery-pytest.log', 'discovery-pytest.xml', 'discovery-swift-syntax.json', 'discovery-static-check.log']},
    'swift_working_tree_sha256': {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
}
(review / 'discovery-validation.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps({k: v for k, v in result.items() if not k.endswith('sha256')}, indent=2))
assert result['pytest_failures'] == result['pytest_errors'] == 0
