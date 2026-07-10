#!/usr/bin/env python3
"""
Record the latest app version to Supabase core_config table.

Called from CI (flutter-distribute.yml) after a successful APK build.
Writes version_code, version_name, download_url to the 'app_version'
key in core_config so the /api/app-version backend endpoint can read it
without hitting the GitHub API.

Environment variables:
  SUPABASE_URL          — Supabase project URL
  SUPABASE_SERVICE_ROLE_KEY — service_role key (bypasses RLS)
  VERSION_CODE          — integer build number (e.g. 3)
  VERSION_NAME          — semantic version string (e.g. 1.0.0)
  DOWNLOAD_URL          — URL to download the APK
  RELEASE_NOTES         — optional release notes string
"""
import os
import json
import urllib.request
import urllib.error
import sys


def main():
    supabase_url = os.environ.get("SUPABASE_URL")
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    version_code = os.environ.get("VERSION_CODE")
    version_name = os.environ.get("VERSION_NAME")
    download_url = os.environ.get("DOWNLOAD_URL")
    release_notes = os.environ.get("RELEASE_NOTES", "")

    missing = []
    for var, name in [
        (supabase_url, "SUPABASE_URL"),
        (service_role_key, "SUPABASE_SERVICE_ROLE_KEY"),
        (version_code, "VERSION_CODE"),
        (version_name, "VERSION_NAME"),
        (download_url, "DOWNLOAD_URL"),
    ]:
        if not var:
            missing.append(name)

    if missing:
        print(f"❌ Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    payload = json.dumps({
        "key": "app_version",
        "content": json.dumps({
            "version_code": int(version_code),
            "version_name": version_name,
            "download_url": download_url,
            "release_notes": release_notes,
        }),
    }).encode()

    # Use on_conflict=key to trigger upsert via the unique constraint on core_config.key
    url = f"{supabase_url}/rest/v1/core_config?on_conflict=key"
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    req = urllib.request.Request(url, data=payload, headers=headers)

    try:
        urllib.request.urlopen(req)
        print(f"✅ Recorded version {version_name} (build {version_code}) in Supabase")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        print(f"❌ HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
