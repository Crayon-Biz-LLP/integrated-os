#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  build_apk.sh — Build release APK with auto-versioning
# ─────────────────────────────────────────────────────────────────────────────
#  Usage:
#    ./build_apk.sh              # Build with auto-version from git
#    ./build_apk.sh --release    # Same (default)
#    ./build_apk.sh --debug      # Debug APK (skips signing)
#
#  Versioning:
#    versionName = 1.0.1        (from pubspec.yaml base)
#    versionCode = git commit count  (always increments, never manual)
#
#  The resulting APK is at: build/app/outputs/flutter-apk/app-release.apk
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Detect build mode ──
MODE="${1:-release}"
case "$MODE" in
  --release|-r|release)
    MODE="release"
    APK_SUFFIX="release"
    FLUTTER_MODE="--release"
    ;;
  --debug|-d|debug)
    MODE="debug"
    APK_SUFFIX="debug"
    FLUTTER_MODE=""
    ;;
  *)
    echo "Usage: $0 [--release|--debug]"
    exit 1
    ;;
esac

# ── Read version name from pubspec.yaml ──
VERSION_NAME=$(grep '^version:' pubspec.yaml | sed 's/version: *//' | sed 's/+.*//')
if [ -z "$VERSION_NAME" ]; then
  echo "❌ Could not read version from pubspec.yaml"
  exit 1
fi

# ── Generate version code from git commit count ──
BUILD_NUMBER=$(git rev-list --count HEAD)
if [ -z "$BUILD_NUMBER" ]; then
  echo "❌ Could not determine git commit count"
  exit 1
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Building Rhodey APK"
echo "  Mode:        $MODE"
echo "  Version:     $VERSION_NAME ($BUILD_NUMBER)"
echo "  Output:      build/app/outputs/flutter-apk/app-${APK_SUFFIX}.apk"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Build ──
flutter build apk $FLUTTER_MODE \
  --build-name="$VERSION_NAME" \
  --build-number="$BUILD_NUMBER"

echo ""
echo "✅ Done! Version $VERSION_NAME (build $BUILD_NUMBER)"
echo "   APK: build/app/outputs/flutter-apk/app-${APK_SUFFIX}.apk"
