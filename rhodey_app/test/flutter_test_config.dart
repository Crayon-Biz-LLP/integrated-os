/// Test-wide configuration for `flutter test` under `test/`.
///
/// ## Why a per-platform golden comparator
///
/// The widget-test engine cannot pin emoji glyphs cross-platform: for emoji
/// codepoints it prefers the SYSTEM emoji font over any `fontFamilyFallback`
/// (Flutter issue #84631 — Noto Color Emoji ships on GitHub's ubuntu runners,
/// Apple's on macOS). The committed `NotoEmoji-Regular.ttf` (loaded via
/// FontLoader in the golden tests) is honored on macOS but ignored on Linux,
/// so a single golden PNG can never match both platforms byte-for-byte.
///
/// The fix (standard Flutter technique, cf. golden_toolkit): keep SEPARATE
/// golden masters per platform. On Linux, the comparator rewrites
/// `briefing_card.png` → `briefing_card.linux.png`; every other platform uses
/// the plain name. Each platform compares against its own committed master,
/// so goldens are deterministic everywhere — and real emoji render in both.
///
/// ## Regeneration flow (an INTENTIONAL visual change to a golden card)
///
/// macOS (dev machine):
///     cd rhodey_app && flutter test --update-goldens test/goldens/
/// → rewrites `briefing_card.png` / `task_ack_card.png`.
///
/// Linux (CI runs the pixel compare, so the Linux master must move too):
///     dispatch `.github/workflows/generate_linux_goldens.yml` (manual)
///     → downloads `linux-goldens` artifacts
///     → commit the `*.linux.png` files alongside the macOS masters.
///
/// If you commit a macOS-only golden change, Linux CI fails with a visible
/// pixel diff on the stale `*.linux.png` — that is the guard working, not a
/// bug: it forces the platform masters to move together.
library;

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';

/// The host OS the test process runs on. Golden masters are per-platform:
/// Linux CI uses `*.linux.png` (see the header note); every other platform
/// keeps the plain golden name.
bool get _isLinux => Platform.operatingSystem == 'linux';

/// Resolves the golden key to its platform-specific master, e.g.
/// `briefing_card.png` → `briefing_card.linux.png` on Linux.
///
/// The [golden] URI at this point is the relative key from
/// `matchesGoldenFile(...)` (LocalFileComparator joins it with its basedir).
Uri _platformGoldenUri(Uri golden) {
  if (!_isLinux) {
    return golden;
  }
  final String path = golden.path;
  final int dot = path.lastIndexOf('.');
  if (dot <= 0) {
    return golden; // no extension to suffix — leave untouched
  }
  final String newPath = '${path.substring(0, dot)}.linux${path.substring(dot)}';
  return golden.replace(path: newPath);
}

/// [LocalFileComparator] that compares/updates against the platform-specific
/// golden master (`*.linux.png` on Linux, plain name elsewhere).
class PlatformAwareGoldenComparator extends LocalFileComparator {
  PlatformAwareGoldenComparator(super.testFile);

  @override
  Future<bool> compare(Uint8List imageBytes, Uri golden) =>
      super.compare(imageBytes, _platformGoldenUri(golden));

  @override
  Future<void> update(Uri golden, Uint8List imageBytes) =>
      super.update(_platformGoldenUri(golden), imageBytes);
}

/// Flutter runs this before the tests in this directory (and subdirectories).
/// The framework has already installed a [LocalFileComparator] pointing at the
/// test file, so we borrow its basedir and swap in the platform-aware version.
Future<void> testExecutable(FutureOr<void> Function() testMain) async {
  final GoldenFileComparator current = goldenFileComparator;
  if (current is LocalFileComparator) {
    // basedir already ends with a separator; resolving a placeholder name
    // reproduces the SAME basedir on the new comparator.
    goldenFileComparator = PlatformAwareGoldenComparator(
      current.basedir.resolve('placeholder_test.dart'),
    );
  }
  await testMain();
}
