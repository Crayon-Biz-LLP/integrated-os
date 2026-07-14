# Flutter APK Versioning & Distribution

Automated APK build pipeline for the Rhodey Flutter app.

## Pipeline (`.github/workflows/flutter-distribute.yml`)
1. On workflow dispatch, checks out the repo and sets up Flutter.
2. Reads version name and code from `pubspec.yaml`.
3. Builds a signed release APK using Android keystore from GitHub Secrets.
4. Creates a GitHub Release with the APK attached, using the version name as the release title.
5. The Flutter app's `update_service.dart` checks for new releases and offers in-app download/install.

## Version Management
- Source of truth: `pubspec.yaml` (`version: 1.0.0+1`)
- CI injects version name/code into the APK manifest automatically
- In-app update compares release title against current version

## Key Files
- `.github/workflows/flutter-distribute.yml` — CI pipeline
- `rhodey_app/lib/services/update_service.dart` — In-app update logic
- `rhodey_app/pubspec.yaml` — Version source of truth
