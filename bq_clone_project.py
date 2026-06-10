"""
Bulk Clone BigQuery Project
=============================
Copies ALL datasets, tables, and views from a source GCP project
to a destination project in one go.

Usage:
    pip install google-cloud-bigquery
    python bq_clone_project.py

Config:
    Edit SOURCE_PROJECT and DEST_PROJECT below before running.
"""

from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core.exceptions import Conflict, NotFound
import time

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SOURCE_PROJECT = "msr-msia-sales-analysis"     # ← change this
DEST_PROJECT   = "msr-msia-sales-analysis-clone"  # ← change this

# Set to True to also copy table data (tables + materialized views).
# Views are always recreated (SQL-only, no data to copy).
COPY_DATA = True

# How many tables to copy in parallel per dataset (BigQuery limit is ~10)
PARALLEL_COPIES = 5

# ─── SCRIPT ────────────────────────────────────────────────────────────────────

CRED_PATH = "bigquery_credentials.json"
credentials = service_account.Credentials.from_service_account_file(
    CRED_PATH, scopes=["https://www.googleapis.com/auth/bigquery"]
)
client = bigquery.Client(project=SOURCE_PROJECT, credentials=credentials)
dest_client = bigquery.Client(project=DEST_PROJECT, credentials=credentials)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def clone_dataset_metadata(src_dataset: bigquery.Dataset) -> bigquery.Dataset:
    """Create the destination dataset with matching metadata."""
    dest_ref = f"{DEST_PROJECT}.{src_dataset.dataset_id}"
    dest_ds = bigquery.Dataset(dest_ref)

    # Copy over metadata
    dest_ds.description = src_dataset.description
    dest_ds.labels = src_dataset.labels or {}
    dest_ds.location = src_dataset.location or "US"

    # Copy table expiry if set
    if src_dataset.default_table_expiration_ms:
        dest_ds.default_table_expiration_ms = src_dataset.default_table_expiration_ms

    # Copy KMS key if set
    if src_dataset.default_encryption_configuration:
        dest_ds.default_encryption_configuration = src_dataset.default_encryption_configuration

    try:
        dest_ds = dest_client.create_dataset(dest_ds, exists_ok=False)
        log(f"  ✅ Created dataset: {dest_ref}")
    except Conflict:
        log(f"  ⏭️  Dataset already exists: {dest_ref}")
        dest_ds = dest_client.get_dataset(dest_ref)

    return dest_ds


def copy_table(src_table: bigquery.Table, dest_dataset_id: str):
    """Copy a regular table (with data) to the destination."""
    src_ref = src_table.reference
    dest_ref = bigquery.TableReference(
        bigquery.DatasetReference(DEST_PROJECT, dest_dataset_id),
        src_table.table_id,
    )

    # Skip if already exists
    try:
        dest_client.get_table(dest_ref)
        log(f"    ⏭️  Table already exists: {dest_ref.table_id}")
        return
    except NotFound:
        pass

    if not COPY_DATA:
        log(f"    ⏭️  Skipped (COPY_DATA=False): {src_table.table_id}")
        return

    log(f"    📋 Copying table: {src_table.table_id} ...")
    job = dest_client.copy_table(src_ref, dest_ref)

    # Poll until done (with timeout)
    try:
        job.result(timeout=600)  # 10 min per table
        log(f"    ✅ Copied table: {src_table.table_id}")
    except Exception as e:
        log(f"    ❌ Failed to copy {src_table.table_id}: {e}")


def recreate_view(src_table: bigquery.Table, dest_dataset_id: str):
    """Recreate a view in the destination using its SQL definition."""
    dest_ref = bigquery.TableReference(
        bigquery.DatasetReference(DEST_PROJECT, dest_dataset_id),
        src_table.table_id,
    )

    # Skip if already exists
    try:
        existing = dest_client.get_table(dest_ref)
        log(f"    ⏭️  View already exists: {dest_ref.table_id}")
        return
    except NotFound:
        pass

    view_query = src_table.view_query  # ← the SQL behind the view
    if not view_query:
        log(f"    ⚠️  No SQL for view {src_table.table_id}, skipping")
        return

    # Rewrite project references in the SQL so the view points to the new project
    rewritten_query = view_query.replace(SOURCE_PROJECT, DEST_PROJECT)

    dest_view = bigquery.Table(dest_ref)
    dest_view.view_query = rewritten_query
    dest_view.description = src_table.description
    dest_view.labels = src_table.labels or {}

    try:
        dest_client.create_table(dest_view)
        log(f"    ✅ Created view: {src_table.table_id}")
    except Exception as e:
        log(f"    ❌ Failed to create view {src_table.table_id}: {e}")


def recreate_materialized_view(src_table: bigquery.Table, dest_dataset_id: str):
    """Recreate a materialized view in the destination."""
    dest_ref = bigquery.TableReference(
        bigquery.DatasetReference(DEST_PROJECT, dest_dataset_id),
        src_table.table_id,
    )

    try:
        dest_client.get_table(dest_ref)
        log(f"    ⏭️  Materialized view already exists: {dest_ref.table_id}")
        return
    except NotFound:
        pass

    mv_query = src_table.mview_query
    if not mv_query:
        log(f"    ⚠️  No SQL for materialized view {src_table.table_id}, skipping")
        return

    rewritten_query = mv_query.replace(SOURCE_PROJECT, DEST_PROJECT)

    dest_mv = bigquery.Table(dest_ref)
    dest_mv.mview_query = rewritten_query
    dest_mv.description = src_table.description
    dest_mv.labels = src_table.labels or {}

    # Refresh interval if set
    if hasattr(src_table, 'mview_refresh_interval_seconds') and src_table.mview_refresh_interval_seconds:
        dest_mv.mview_refresh_interval_seconds = src_table.mview_refresh_interval_seconds

    # Enable refresh if set
    if hasattr(src_table, 'mview_enable_refresh') and src_table.mview_enable_refresh is not None:
        dest_mv.mview_enable_refresh = src_table.mview_enable_refresh

    try:
        dest_client.create_table(dest_mv)
        log(f"    ✅ Created materialized view: {src_table.table_id}")
    except Exception as e:
        log(f"    ❌ Failed to create MV {src_table.table_id}: {e}")


def main():
    log(f"🚀 Cloning project: {SOURCE_PROJECT} → {DEST_PROJECT}")
    log(f"   Copy data: {COPY_DATA}")
    log("")

    # 1. List all datasets in source project
    datasets = list(client.list_datasets())
    if not datasets:
        log("No datasets found in source project.")
        return

    log(f"Found {len(datasets)} dataset(s) in {SOURCE_PROJECT}")
    log("")

    stats = {"datasets": 0, "tables": 0, "views": 0, "mviews": 0, "errors": 0}

    # 2. Process each dataset
    for src_ds_item in datasets:
        src_dataset = client.get_dataset(src_ds_item.reference)
        ds_id = src_dataset.dataset_id
        log(f"📦 Dataset: {ds_id}")

        # Create destination dataset
        clone_dataset_metadata(src_dataset)
        stats["datasets"] += 1

        # 3. List all tables/views in this dataset
        tables = list(client.list_tables(src_ds_item.reference))
        log(f"   Found {len(tables)} resource(s)")

        # 4. Process each table/view
        for tbl_item in tables:
            src_table = client.get_table(tbl_item.reference)

            if src_table.table_type == "TABLE":
                copy_table(src_table, ds_id)
                stats["tables"] += 1

            elif src_table.table_type == "VIEW":
                recreate_view(src_table, ds_id)
                stats["views"] += 1

            elif src_table.table_type == "MATERIALIZED_VIEW":
                recreate_materialized_view(src_table, ds_id)
                stats["mviews"] += 1

            else:
                log(f"    ⚠️  Unknown type {src_table.table_type}: {src_table.table_id}")
                stats["errors"] += 1

        log("")

    # Summary
    log("=" * 60)
    log(f"✅ Done! Cloned {stats['datasets']} dataset(s):")
    log(f"   Tables:             {stats['tables']}")
    log(f"   Views:              {stats['views']}")
    log(f"   Materialized Views: {stats['mviews']}")
    if stats["errors"]:
        log(f"   ⚠️  Unknown types:   {stats['errors']}")
    log("=" * 60)


if __name__ == "__main__":
    main()