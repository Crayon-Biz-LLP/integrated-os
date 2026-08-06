#!/usr/bin/env python3
"""
Upload APK to Supabase Storage and record version info in core_config.

Called from build_apk.sh after a successful build.
Creates the 'apk-builds' bucket if it doesn't exist (public).
Uploads the APK, then records version_code, version_name, and the
public download URL to core_config so the in-app UpdateService can
find it via GET /api/app-version.

Environment variables:
  SUPABASE_URL               — Supabase project URL (required)
  SUPABASE_SERVICE_ROLE_KEY  — service_role key (required, bypasses RLS)
  VERSION_CODE               — integer build number from git commit count
  VERSION_NAME               — semantic version string (e.g. 1.0.1)
  APK_PATH                   — path to the APK file (default: build/app/outputs/flutter-apk/app-release.apk)
  RELEASE_NOTES              — optional release notes string
"""
import os
import json
import sys
import ssl
import mimetypes
import urllib.request
import urllib.error


BUCKET_NAME = "apk-builds"
OBJECT_NAME = "app-release.apk"


def _create_ssl_context():
    """Create an SSL context for Supabase API calls.

    macOS Python sometimes lacks system CA certificates, causing
    SSL: CERTIFICATE_VERIFY_FAILED. We try certifi first, then
    fall back to the default context.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _urlopen(url_or_req, **kwargs):
    """Wrapper around urllib.request.urlopen with custom SSL context."""
    ctx = _create_ssl_context()
    return urllib.request.urlopen(url_or_req, context=ctx, **kwargs)


def main():
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    version_code = os.environ.get("VERSION_CODE", "")
    version_name = os.environ.get("VERSION_NAME", "")
    apk_path = os.environ.get("APK_PATH",
        "build/app/outputs/flutter-apk/app-release.apk")
    release_notes = os.environ.get("RELEASE_NOTES", "")

    missing = []
    for var, name in [
        (supabase_url, "SUPABASE_URL"),
        (service_role_key, "SUPABASE_SERVICE_ROLE_KEY"),
        (version_code, "VERSION_CODE"),
        (version_name, "VERSION_NAME"),
    ]:
        if not var:
            missing.append(name)
    if missing:
        print(f"❌ Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    if not os.path.exists(apk_path):
        print(f"❌ APK not found at: {apk_path}")
        print("   Build the APK first with: ./build_apk.sh")
        sys.exit(1)

    apk_size = os.path.getsize(apk_path)
    print(f"📦 APK: {apk_path} ({apk_size / 1024 / 1024:.1f} MB)")
    print(f"📋 Version: {version_name} (build {version_code})")

    # ── Step 1: Ensure bucket exists —───────────────────────────────────────
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }

    # Create the bucket if it doesn't exist, with a 200MB file size limit
    # (Supabase free tier defaults to 50MB, APK is ~54MB).
    create_payload = json.dumps({
        "name": BUCKET_NAME,
        "public": True,
        "file_size_limit": 209715200,  # 200MB
    }).encode()
    create_req = urllib.request.Request(
        f"{supabase_url}/storage/v1/bucket",
        data=create_payload,
        headers=headers,
        method="POST",
    )
    try:
        _urlopen(create_req)
        print(f"✅ Created public bucket '{BUCKET_NAME}' (200MB limit)")
    except urllib.error.HTTPError as e:
        # 409 Conflict means bucket already exists — update it instead
        if e.code == 409:
            print(f"✅ Bucket '{BUCKET_NAME}' already exists — updating file size limit...")
            update_payload = json.dumps({
                "public": True,
                "file_size_limit": 209715200,
            }).encode()
            update_req = urllib.request.Request(
                f"{supabase_url}/storage/v1/bucket/{BUCKET_NAME}",
                data=update_payload,
                headers=headers,
                method="PUT",
            )
            try:
                _urlopen(update_req)
                print(f"✅ Bucket '{BUCKET_NAME}' updated (200MB limit)")
            except Exception:
                print("⚠️ Could not update bucket limit — may need manual Supabase dashboard config")
        else:
            error_body = e.read().decode()
            print(f"⚠️  Bucket creation note: HTTP {e.code}: {error_body}")
            print(f"✅ Using bucket '{BUCKET_NAME}'")
    except Exception as e:
        print(f"⚠️  Bucket setup note: {e}")
        print("   Continuing with upload attempt...")

    # ── Step 2: Upload APK to Supabase Storage ──────────────────────────────
    upload_url = f"{supabase_url}/storage/v1/object/{BUCKET_NAME}/{OBJECT_NAME}"

    # Detect content type
    content_type, _ = mimetypes.guess_type(apk_path)
    if not content_type:
        content_type = "application/vnd.android.package-archive"

    with open(apk_path, "rb") as f:
        apk_data = f.read()

    upload_headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": content_type,
        "x-upsert": "true",  # Overwrite existing file
    }

    upload_req = urllib.request.Request(
        upload_url,
        data=apk_data,
        headers=upload_headers,
        method="POST",
    )

    try:
        _urlopen(upload_req)
        print("✅ APK uploaded to Supabase Storage")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        if "Payload too large" in error_body or e.code == 413:
            print("❌ Supabase Storage free tier limits file size to 50MB.")
            print(f"   APK is {apk_size / 1024 / 1024:.1f}MB — too large.")
            print("")
            print("🔧 To fix this, either:")
            print("   1. Increase the upload limit in Supabase Dashboard → Storage → Settings")
            print("   2. Or use a different storage (Cloudflare R2, S3, etc.)")
            print("")
            print(f"📌 The APK was BUILT SUCCESSFULLY at: {apk_path}")
            print("   You can manually upload it to your APK host and run: ")
            print(f"   export VERSION_CODE={version_code} VERSION_NAME={version_name} DOWNLOAD_URL=<your-url>")
            print("   python3 scripts/record_app_version.py")
        else:
            print(f"❌ Upload failed: HTTP {e.code}: {error_body}")
        sys.exit(1)

    # ── Step 3: Build the public download URL ────────────────────────────────
    download_url = f"{supabase_url}/storage/v1/object/public/{BUCKET_NAME}/{OBJECT_NAME}"
    print(f"🔗 Public URL: {download_url}")

    # ── Step 4: Record version in core_config ────────────────────────────────
    payload = json.dumps({
        "key": "app_version",
        "content": json.dumps({
            "version_code": int(version_code),
            "version_name": version_name,
            "download_url": download_url,
            "release_notes": release_notes,
        }),
    }).encode()

    config_url = f"{supabase_url}/rest/v1/core_config?on_conflict=key"
    config_headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    config_req = urllib.request.Request(
        config_url, data=payload, headers=config_headers, method="POST",
    )

    try:
        _urlopen(config_req)
        print(f"✅ Version {version_name} (build {version_code}) recorded in Supabase")
    except urllib.error.HTTPError as e:
        print(f"❌ Version recording failed: HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)

    print("\n🎉 Done! In-app update checker will find this version on next app open.")
    print(f"   Open the app → it will detect v{version_name} → start download → install.")


if __name__ == "__main__":
    main()
