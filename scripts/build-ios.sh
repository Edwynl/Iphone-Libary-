#!/usr/bin/env bash
set -euo pipefail
if [[ "$(uname)" != "Darwin" ]]; then
  echo "iOS builds require macOS with Xcode (not Windows/Linux)." >&2
  exit 1
fi
command -v xcodegen >/dev/null || { echo "Install XcodeGen: brew install xcodegen" >&2; exit 1; }
command -v xcodebuild >/dev/null || { echo "Install full Xcode and select it with xcode-select" >&2; exit 1; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/ios/PhotosCompanion"
xcodegen generate
xcodebuild -project PhotosCompanion.xcodeproj -scheme PhotosCompanion \
  -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  CODE_SIGNING_ALLOWED=NO build
