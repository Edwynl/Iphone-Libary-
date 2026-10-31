"""Optional local grammar check. Requires tree-sitter and tree-sitter-swift.

This cannot validate Swift types, SDK APIs, linking, signing or runtime behavior.
"""
import argparse
import json
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_swift

root = Path(__file__).resolve().parents[2]
options = argparse.ArgumentParser()
options.add_argument('--output', type=Path, default=root / 'docs/review/next-swift-syntax.json')
args = options.parse_args()
parser = Parser(Language(tree_sitter_swift.language()))
files = []
for path in sorted((root / 'ios').rglob('*.swift')):
    if '.build' in path.parts:
        continue
    tree = parser.parse(path.read_bytes())
    files.append({'file': path.relative_to(root).as_posix(), 'has_error': tree.root_node.has_error})
result = {'parser': 'tree-sitter-swift 0.7.4',
          'scope': 'Grammar only. Xcode/type/API/device behavior Not Verified.', 'files': files}
target = args.output
target.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, indent=2))
raise SystemExit(any(item['has_error'] for item in files))
