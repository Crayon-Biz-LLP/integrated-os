# 77 — iOS TestFlight Beta

Ship the Flutter app (`rhodey_app`) to iOS testers via TestFlight from the
existing public GitHub repo — **$0 cost** (public repos get free standard
macOS runners).

## What exists now

- `rhodey_app/ios/` — generated platform scaffolding (bundle ID
  `com.crayon.rhodey_app`, deployment target iOS 13, mic + photo-library
  usage strings in `Info.plist`).
- `.github/workflows/flutter-distribute-ios.yml` — build + TestFlight upload
  on `macos-latest`, triggered on push to `main` (paths: `rhodey_app/**`) or
  manually via **Actions → workflow_dispatch**.
- App code fixes needed for iOS to behave:
  - `notification_service.dart` — registers push tokens with the real
    platform (`ios` vs `android`) so the backend sends APNs payloads, and
    shows foreground pushes as iOS local notifications.
  - `update_service.dart` — the APK in-app updater is Android-only now; iOS
    testers get updates through TestFlight instead of a bogus "update" dialog.

## Signing approach (why no fastlane / no cert files)

The workflow drives `xcodebuild` directly with Xcode 15+ headless automatic
signing: `-allowProvisioningUpdates -authenticationKeyPath/-authenticationKeyID/-authenticationKeyIssuerID`
let the **App Store Connect API key** create/download the distribution
certificate and App Store provisioning profile on the fly. No certificates,
no provisioning profiles, no `.p12`, no fastlane match storage — and renewal
is Apple/Xcode's problem, not ours. Upload uses `xcrun altool --upload-app`
with the same API key.

Requirement: the API key needs the **App Manager** role (or higher) so it can
manage certificates/profiles.

## One-time account setup (user steps)

1. **App Store Connect** — [appstoreconnect.apple.com](https://appstoreconnect.apple.com)
   → *Apps* → *+* → **register bundle ID `com.crayon.rhodey_app`** → name it
   *Rhodey*. (Registering the app creates the bundle ID in the developer
   portal.) The first automatic-signing build needs this record to exist.
2. **Firebase** — [console.firebase.google.com](https://console.firebase.google.com)
   → project `rhodey-os` → *Project Settings → Add app → iOS* → bundle ID
   `com.crayon.rhodey_app` → register → **download `GoogleService-Info.plist`**.
3. **App Store Connect API key** — App Store Connect → *Users and Access →
   Integrations → App Store Connect API* → **+** (role: **App Manager**) →
   note the **Key ID** and **Issuer ID**, download the **.p8** file.
4. **Team ID** — App Store Connect → *Membership details* (10-character ID).
5. **Add repo secrets** — GitHub → repo → *Settings → Secrets and variables →
   Actions → New repository secret*:

   | Secret | Value |
   |---|---|
   | `APPLE_API_KEY` | **base64** of the `.p8` file: `base64 -i AuthKey_XXXXXXXX.p8 \| tr -d '\n'` |
   | `APPLE_API_KEY_ID` | the key ID from step 3 |
   | `APPLE_API_ISSUER_ID` | the issuer ID from step 3 |
   | `APPLE_TEAM_ID` | team ID from step 4 |
   | `GOOGLE_SERVICE_INFO_PLIST` | full contents of `GoogleService-Info.plist` |

## After the first build lands

- Watch the build at **Actions → "Flutter — Build & Distribute (iOS TestFlight)"**.
- First upload: App Store Connect → *Apps → Rhodey → TestFlight* — the build
  appears as *Processing*, then becomes *Missing Compliance* (tap it and
  answer "No" to export compliance) before testers can install it.
- Add testers: TestFlight → *Internal Testing* group → *+* → their Apple IDs
  (up to 100 internal testers, no beta review).

## Known gaps / follow-ups (not blockers for the beta)

- **Push on iOS** needs the **APNs key** added in Firebase (Project Settings →
  Cloud Messaging → iOS app configuration → APNs Authentication Key) before
  FCM can reach iOS devices. Without it the app still runs; pushes just won't
  arrive. Do this after the first build works.
- **App icon** is the default Flutter icon — replace
  `ios/Runner/Assets.xcassets/AppIcon.appiconset/` with the real artwork
  before wide release.
- **Share-into-Rhodey** (`receive_sharing_intent`) and the **home widget**
  (`home_widget`) are Android-only for now — both are safe no-ops on iOS, but
  the iOS share extension / widget extension are separate native targets not
  yet set up.
- The build number is derived from git history (`git rev-list --count HEAD`),
  same as Android — it increases on every push, which TestFlight requires.

## Cost & cadence

- **$0**: `macos-latest` standard runners are free on public repos; larger
  runners are not (this workflow uses the standard one).
- ~15–25 min per build (pod install + Xcode compile). Free on public repos.
- Runs on every push touching `rhodey_app/**` plus manual trigger.
