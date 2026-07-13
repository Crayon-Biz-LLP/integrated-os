#!/usr/bin/env python3
"""Sync .md bundles to Google Docs in Drive, then Notebook LM auto-syncs.

First run: creates Google Docs from .md bundles (Drive API import).
Subsequent runs: updates Doc content in-place (Drive API if it works,
otherwise Google Docs API batchUpdate).

Usage:
    python3 scripts/sync_notebooklm_docs.py
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

FOLDER_NAME = "NotebookLM Codebase Sources"

# Parent folder ID for Crayon/Rhodey OS/ — hardcoded because drive.file scope
# can't discover it but can still create children inside it.
RHODEY_OS_FOLDER_ID = "1fOk_nKy5jjEKVKel88ZK47RR6KJjN3lJ"


def get_creds():
    from core.services.google_service import get_google_creds
    return get_google_creds()


def get_or_create_folder(drive):
    query = f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = drive.files().list(q=query, fields="files(id)", pageSize=1).execute()
    for f in results.get("files", []):
        return f["id"]
    folder = drive.files().create(
        body={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder", "parents": [RHODEY_OS_FOLDER_ID]},
        fields="id",
    ).execute()
    return folder["id"]


def update_doc_via_docs_api(doc_id, text, creds):
    svc = build("docs", "v1", credentials=creds)
    doc = svc.documents().get(documentId=doc_id).execute()
    end = doc["body"]["content"][-1]["endIndex"]
    svc.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end - 1}}},
                {"insertText": {"location": {"index": 1}, "text": text}},
            ]
        },
    ).execute()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles-dir", default="notebooklm")
    parser.add_argument("--id-map", default="notebooklm/.doc_ids.json")
    args = parser.parse_args()

    repo_root = Path.cwd()
    bundles_dir = Path(args.bundles_dir)
    if not bundles_dir.is_absolute():
        bundles_dir = repo_root / bundles_dir
    id_map_path = Path(args.id_map)
    if not id_map_path.is_absolute():
        id_map_path = repo_root / id_map_path

    creds = get_creds()
    drive = build("drive", "v3", credentials=creds)
    folder_id = get_or_create_folder(drive)

    doc_ids = {}
    if id_map_path.exists():
        doc_ids = json.loads(id_map_path.read_text())

    bundles = sorted(bundles_dir.glob("*.md"))
    docs_scope_missing = False

    for bundle in bundles:
        title = bundle.stem
        if title == "_index":
            continue

        print(f"  {title}.md...", end=" ", flush=True)

        if title in doc_ids:
            try:
                drive.files().update(
                    fileId=doc_ids[title],
                    media_body=MediaFileUpload(str(bundle), mimetype="text/plain", resumable=True),
                ).execute()
                print("updated")
            except HttpError as e:
                if e.resp.status in (400, 403):
                    print(f"trying Docs API...", end=" ", flush=True)
                    try:
                        update_doc_via_docs_api(doc_ids[title], bundle.read_text(encoding="utf-8"), creds)
                        print("ok")
                        docs_scope_missing = True
                    except HttpError as e2:
                        if e2.resp.status == 403 and "docs" in str(e2):
                            docs_scope_missing = True
                            print("needs Docs OAuth scope")
                        else:
                            raise
                else:
                    raise
        else:
            body = {
                "name": title,
                "mimeType": "application/vnd.google-apps.document",
                "parents": [folder_id],
            }
            media = MediaFileUpload(str(bundle), mimetype="text/plain", resumable=True)
            doc = drive.files().create(body=body, media_body=media, fields="id").execute()
            doc_ids[title] = doc["id"]
            print("created")

    if docs_scope_missing:
        print(
            "\nNote: Some updates used the Google Docs API, which requires "
            "the 'https://www.googleapis.com/auth/documents' OAuth scope.\n"
            "Run 'python3 scripts/update_google_oauth.py' once to grant it."
        )

    id_map_path.parent.mkdir(parents=True, exist_ok=True)
    id_map_path.write_text(json.dumps(doc_ids, indent=2))

    count = len([b for b in bundles if b.stem != "_index"])
    print(f"\nDone — {count} Docs synced to '{FOLDER_NAME}'")


if __name__ == "__main__":
    main()
