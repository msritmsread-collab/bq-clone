"""
Recreate all views in Report_MSREAD dataset.
These views failed on the first pass because they reference tables
in other datasets that didn't exist yet. Now they do.
"""

from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core.exceptions import NotFound
import time

SOURCE_PROJECT = "msr-msia-sales-analysis"
DEST_PROJECT   = "msr-msia-sales-analysis-clone"

CRED_PATH = "bigquery_credentials.json"
credentials = service_account.Credentials.from_service_account_file(
    CRED_PATH, scopes=["https://www.googleapis.com/auth/bigquery"]
)
client = bigquery.Client(project=SOURCE_PROJECT, credentials=credentials)
dest_client = bigquery.Client(project=DEST_PROJECT, credentials=credentials)

DATASET_ID = "Report_MSREAD"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def main():
    log(f"🚀 Recreating views in {DATASET_ID}")

    tables = list(client.list_tables(f"{SOURCE_PROJECT}.{DATASET_ID}"))
    log(f"  Found {len(tables)} resource(s)")

    success = 0
    failed = 0

    for tbl_item in tables:
        src_table = client.get_table(tbl_item.reference)

        if src_table.table_type != "VIEW":
            continue

        dest_ref = bigquery.TableReference(
            bigquery.DatasetReference(DEST_PROJECT, DATASET_ID),
            src_table.table_id,
        )

        # Drop existing view if it exists (broken from first pass)
        try:
            dest_client.delete_table(dest_ref)
            log(f"    🗑️  Dropped existing view: {src_table.table_id}")
        except NotFound:
            pass

        view_query = src_table.view_query
        if not view_query:
            log(f"    ⚠️  No SQL for view {src_table.table_id}, skipping")
            failed += 1
            continue

        rewritten = view_query.replace(SOURCE_PROJECT, DEST_PROJECT)

        dest_view = bigquery.Table(dest_ref)
        dest_view.view_query = rewritten
        dest_view.description = src_table.description
        dest_view.labels = src_table.labels or {}

        try:
            dest_client.create_table(dest_view)
            log(f"    ✅ Created view: {src_table.table_id}")
            success += 1
        except Exception as e:
            log(f"    ❌ Failed: {src_table.table_id}: {e}")
            failed += 1

    log("")
    log("=" * 60)
    log(f"✅ Done! {DATASET_ID} views:")
    log(f"   Created: {success}")
    log(f"   Failed:  {failed}")
    log("=" * 60)


if __name__ == "__main__":
    main()