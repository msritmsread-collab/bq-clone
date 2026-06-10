"""
Copy msread_products dataset tables and views.
External tables (Google Sheets) cannot be copied via API.
"""

from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core.exceptions import Conflict, NotFound
import time

SOURCE_PROJECT = "msr-msia-sales-analysis"
DEST_PROJECT   = "msr-msia-sales-analysis-clone"

CRED_PATH = "bigquery_credentials.json"
credentials = service_account.Credentials.from_service_account_file(
    CRED_PATH, scopes=["https://www.googleapis.com/auth/bigquery"]
)
client = bigquery.Client(project=SOURCE_PROJECT, credentials=credentials)
dest_client = bigquery.Client(project=DEST_PROJECT, credentials=credentials)

DATASET_ID = "msread_products"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def main():
    log(f"🚀 Copying {DATASET_ID} dataset")

    # 1. Create dataset if needed
    src_dataset = client.get_dataset(f"{SOURCE_PROJECT}.{DATASET_ID}")
    dest_ref = f"{DEST_PROJECT}.{DATASET_ID}"

    dest_ds = bigquery.Dataset(dest_ref)
    dest_ds.description = src_dataset.description
    dest_ds.labels = src_dataset.labels or {}
    dest_ds.location = src_dataset.location or "asia-southeast1"

    if src_dataset.default_table_expiration_ms:
        dest_ds.default_table_expiration_ms = src_dataset.default_table_expiration_ms

    try:
        dest_ds = dest_client.create_dataset(dest_ds, exists_ok=False)
        log(f"  ✅ Created dataset: {dest_ref}")
    except Conflict:
        log(f"  ⏭️  Dataset already exists: {dest_ref}")
        dest_ds = dest_client.get_dataset(dest_ref)

    # 2. List tables
    tables = list(client.list_tables(f"{SOURCE_PROJECT}.{DATASET_ID}"))
    log(f"  Found {len(tables)} resource(s)")

    stats = {"tables": 0, "views": 0, "external": 0, "errors": 0}

    for tbl_item in tables:
        src_table = client.get_table(tbl_item.reference)

        if src_table.table_type == "TABLE":
            # Copy table with data
            dest_table_ref = bigquery.TableReference(
                bigquery.DatasetReference(DEST_PROJECT, DATASET_ID),
                src_table.table_id,
            )

            # Skip if exists
            try:
                existing = dest_client.get_table(dest_table_ref)
                if existing.num_rows > 0:
                    log(f"    ⏭️  Table already exists: {src_table.table_id} ({existing.num_rows:,} rows)")
                    stats["tables"] += 1
                    continue
            except NotFound:
                pass

            log(f"    📋 Copying table: {src_table.table_id} ...")
            job = dest_client.copy_table(src_table.reference, dest_table_ref)
            try:
                job.result(timeout=600)
                rows = dest_client.get_table(dest_table_ref).num_rows
                log(f"    ✅ Copied table: {src_table.table_id} ({rows:,} rows)")
                stats["tables"] += 1
            except Exception as e:
                log(f"    ❌ Failed: {src_table.table_id}: {e}")
                stats["errors"] += 1

        elif src_table.table_type == "VIEW":
            dest_table_ref = bigquery.TableReference(
                bigquery.DatasetReference(DEST_PROJECT, DATASET_ID),
                src_table.table_id,
            )

            try:
                dest_client.get_table(dest_table_ref)
                log(f"    ⏭️  View already exists: {src_table.table_id}")
                stats["views"] += 1
                continue
            except NotFound:
                pass

            view_query = src_table.view_query
            if not view_query:
                log(f"    ⚠️  No SQL for view {src_table.table_id}, skipping")
                continue

            rewritten = view_query.replace(SOURCE_PROJECT, DEST_PROJECT)

            dest_view = bigquery.Table(dest_table_ref)
            dest_view.view_query = rewritten
            dest_view.description = src_table.description
            dest_view.labels = src_table.labels or {}

            try:
                dest_client.create_table(dest_view)
                log(f"    ✅ Created view: {src_table.table_id}")
                stats["views"] += 1
            except Exception as e:
                log(f"    ❌ Failed view {src_table.table_id}: {e}")
                stats["errors"] += 1

        elif src_table.table_type == "EXTERNAL":
            log(f"    ⚠️  External (Google Sheet): {src_table.table_id} — skipped")
            stats["external"] += 1

        else:
            log(f"    ⚠️  Unknown type {src_table.table_type}: {src_table.table_id}")
            stats["errors"] += 1

    log("")
    log("=" * 60)
    log(f"✅ Done! {DATASET_ID}:")
    log(f"   Tables:    {stats['tables']}")
    log(f"   Views:     {stats['views']}")
    log(f"   External:  {stats['external']} (skipped — Google Sheets)")
    log(f"   Errors:    {stats['errors']}")
    log("=" * 60)


if __name__ == "__main__":
    main()