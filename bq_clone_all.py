"""
BigQuery Project Cloner — All-in-One
======================================
Clones ALL datasets, tables, views from msr-msia-sales-analysis
to msr-msia-sales-analysis-clone, including FK table fixes
and view recreation.

Runs on GCP VM (Secret Manager) or local Windows (JSON file).

Usage:
    pip install google-cloud-bigquery google-cloud-secret-manager
    python bq_clone_all.py --all          # Run all phases
    python bq_clone_all.py --clone        # Phase 1: main clone (datasets, tables, views)
    python bq_clone_all.py --fk            # Phase 2: fix FK-constraint tables
    python bq_clone_all.py --views         # Phase 3: recreate all failed views
    python bq_clone_all.py --products      # Phase 4: msread_products tables + views
"""

import argparse
import json
import platform
import time

from google.api_core.exceptions import Conflict, NotFound
from google.cloud import bigquery
from google.oauth2 import service_account

# ─── CONFIG ────────────────────────────────────────────────────────────────────
SOURCE_PROJECT = "msr-msia-sales-analysis"
DEST_PROJECT   = "msr-msia-sales-analysis-clone"
SECRET_NAME   = "projects/msr-msia-sales-analysis/secrets/connector-bq-service-account/versions/latest"
CRED_PATH     = "bigquery_credentials.json"

BQ_SCOPE = ["https://www.googleapis.com/auth/bigquery"]

# FK tables by dataset — these can't be copied via copy_table() due to
# cross-project foreign key constraints
FK_TABLES = {
    "fivetran_auri": [
        "abandoned_checkout", "abandoned_checkout_line",
        "abandoned_checkout_shipping_line", "abandoned_checkout_url_tag",
        "app", "collection_product", "customer", "customer_address",
        "customer_tag", "customer_visit", "discount_application",
        "draft_order", "draft_order_line", "draft_order_note_attribute",
        "draft_order_tag", "fulfillment", "fulfillment_order",
        "fulfillment_order_line", "gift_card", "inventory_level",
        "inventory_quantity", "media_image", "media_source",
        "order", "order_adjustment", "order_discount_code",
        "order_line", "order_line_refund", "order_note_attribute",
        "order_risk_assessment", "order_risk_fact", "order_risk_summary",
        "order_shipping_line", "order_tag", "order_url_tag",
        "product_media", "product_option", "product_option_value",
        "product_tag", "product_variant", "product_variant_media",
        "refund", "tender_transaction", "video",
    ],
    "fivetran_msread": [
        "collection_product", "company_location_catalog",
        "customer", "customer_address", "customer_tag", "customer_visit",
        "discount_application",
        "discount_customer_buys_collection", "discount_customer_buys_product",
        "discount_customer_gets_collection", "discount_customer_gets_product",
        "discount_customer_segment_selection", "discount_customer_selection",
        "fulfillment", "fulfillment_event", "fulfillment_order",
        "fulfillment_order_line",
        "inventory_level", "inventory_quantity",
        "order", "order_adjustment", "order_discount_code",
        "order_line", "order_line_refund", "order_note_attribute",
        "order_risk_assessment", "order_risk_fact", "order_risk_summary",
        "order_shipping_line", "order_tag", "order_url_tag",
        "product_media", "product_option", "product_option_value",
        "product_publication", "product_tag", "product_variant",
        "product_variant_media", "publication",
        "refund", "return", "sale", "sales_agreement", "video",
    ],
}


# ─── AUTH ───────────────────────────────────────────────────────────────────────

def get_credentials():
    """Auto-detect auth: Secret Manager on Linux, JSON file on Windows."""
    if platform.system() != "Windows":
        # VM path: use Secret Manager
        try:
            from google.cloud import secretmanager
            sm_client = secretmanager.SecretManagerServiceClient()
            response = sm_client.access_secret_version(request={"name": SECRET_NAME})
            secret_data = json.loads(response.payload.data.decode("UTF-8"))
            creds = service_account.Credentials.from_service_account_info(
                secret_data, scopes=BQ_SCOPE
            )
            print("[AUTH] Using Secret Manager credentials")
            return creds
        except Exception as e:
            print(f"[AUTH] Secret Manager failed ({e}), falling back to JSON file")

    # Local Windows path: use JSON file
    print(f"[AUTH] Using local JSON file: {CRED_PATH}")
    return service_account.Credentials.from_service_account_file(
        CRED_PATH, scopes=BQ_SCOPE
    )


# ─── SHARED STATE ───────────────────────────────────────────────────────────────

credentials = get_credentials()
client     = bigquery.Client(project=SOURCE_PROJECT, credentials=credentials)
dest_client = bigquery.Client(project=DEST_PROJECT, credentials=credentials)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ─── PHASE 1: MAIN CLONE ──────────────────────────────────────────────────────

def phase1_clone():
    """Clone all datasets, tables, views from source to destination."""
    log("=" * 60)
    log("PHASE 1: Full Project Clone")
    log("=" * 60)

    datasets = list(client.list_datasets())
    if not datasets:
        log("No datasets found in source project.")
        return

    log(f"Found {len(datasets)} dataset(s) in {SOURCE_PROJECT}\n")

    stats = {"datasets": 0, "tables": 0, "views": 0, "mviews": 0, "skipped_tables": 0, "errors": 0}

    for src_ds_item in datasets:
        src_dataset = client.get_dataset(src_ds_item.reference)
        ds_id = src_dataset.dataset_id
        log(f"📦 Dataset: {ds_id}")

        # Create destination dataset
        dest_ref = f"{DEST_PROJECT}.{ds_id}"
        dest_ds = bigquery.Dataset(dest_ref)
        dest_ds.description = src_dataset.description
        dest_ds.labels = src_dataset.labels or {}
        dest_ds.location = src_dataset.location or "US"
        if src_dataset.default_table_expiration_ms:
            dest_ds.default_table_expiration_ms = src_dataset.default_table_expiration_ms
        if src_dataset.default_encryption_configuration:
            dest_ds.default_encryption_configuration = src_dataset.default_encryption_configuration

        try:
            dest_ds = dest_client.create_dataset(dest_ds, exists_ok=False)
            log(f"  ✅ Created dataset: {dest_ref}")
        except Conflict:
            log(f"  ⏭️  Dataset already exists: {dest_ref}")
            dest_ds = dest_client.get_dataset(dest_ref)

        stats["datasets"] += 1

        tables = list(client.list_tables(src_ds_item.reference))
        log(f"   Found {len(tables)} resource(s)")

        for tbl_item in tables:
            src_table = client.get_table(tbl_item.reference)

            if src_table.table_type == "TABLE":
                _copy_table(src_table, ds_id, stats)
            elif src_table.table_type == "VIEW":
                _recreate_view(src_table, ds_id, stats)
            elif src_table.table_type == "MATERIALIZED_VIEW":
                _recreate_materialized_view(src_table, ds_id, stats)
            elif src_table.table_type == "EXTERNAL":
                log(f"    ⚠️  External (Google Sheet): {src_table.table_id} — skipped")
                stats["skipped_tables"] += 1
            else:
                log(f"    ⚠️  Unknown type {src_table.table_type}: {src_table.table_id}")
                stats["errors"] += 1

        log("")

    log("=" * 60)
    log(f"✅ Phase 1 Done! Cloned {stats['datasets']} dataset(s):")
    log(f"   Tables:             {stats['tables']}")
    log(f"   Views:              {stats['views']}")
    log(f"   Materialized Views: {stats['mviews']}")
    log(f"   External (skipped): {stats['skipped_tables']}")
    if stats["errors"]:
        log(f"   ⚠️  Unknown types:   {stats['errors']}")
    log("=" * 60)


def _copy_table(src_table, ds_id, stats):
    src_ref = src_table.reference
    dest_ref = bigquery.TableReference(
        bigquery.DatasetReference(DEST_PROJECT, ds_id), src_table.table_id
    )
    try:
        dest_client.get_table(dest_ref)
        log(f"    ⏭️  Table already exists: {src_table.table_id}")
        stats["tables"] += 1
        return
    except NotFound:
        pass

    log(f"    📋 Copying table: {src_table.table_id} ...")
    job = dest_client.copy_table(src_ref, dest_ref)
    try:
        job.result(timeout=600)
        log(f"    ✅ Copied table: {src_table.table_id}")
        stats["tables"] += 1
    except Exception as e:
        log(f"    ❌ Failed to copy {src_table.table_id}: {e}")
        stats["errors"] += 1


def _recreate_view(src_table, ds_id, stats):
    dest_ref = bigquery.TableReference(
        bigquery.DatasetReference(DEST_PROJECT, ds_id), src_table.table_id
    )
    # Drop existing (may be broken)
    try:
        dest_client.delete_table(dest_ref)
    except NotFound:
        pass

    view_query = src_table.view_query
    if not view_query:
        log(f"    ⚠️  No SQL for view {src_table.table_id}, skipping")
        stats["errors"] += 1
        return

    rewritten = view_query.replace(SOURCE_PROJECT, DEST_PROJECT)
    dest_view = bigquery.Table(dest_ref)
    dest_view.view_query = rewritten
    dest_view.description = src_table.description
    dest_view.labels = src_table.labels or {}

    try:
        dest_client.create_table(dest_view)
        log(f"    ✅ Created view: {src_table.table_id}")
        stats["views"] += 1
    except Exception as e:
        log(f"    ❌ Failed to create view {src_table.table_id}: {e}")
        stats["errors"] += 1


def _recreate_materialized_view(src_table, ds_id, stats):
    dest_ref = bigquery.TableReference(
        bigquery.DatasetReference(DEST_PROJECT, ds_id), src_table.table_id
    )
    try:
        dest_client.get_table(dest_ref)
        log(f"    ⏭️  Materialized view already exists: {src_table.table_id}")
        stats["mviews"] += 1
        return
    except NotFound:
        pass

    mv_query = src_table.mview_query
    if not mv_query:
        log(f"    ⚠️  No SQL for materialized view {src_table.table_id}, skipping")
        return

    rewritten = mv_query.replace(SOURCE_PROJECT, DEST_PROJECT)
    dest_mv = bigquery.Table(dest_ref)
    dest_mv.mview_query = rewritten
    dest_mv.description = src_table.description
    dest_mv.labels = src_table.labels or {}
    if hasattr(src_table, 'mview_refresh_interval_seconds') and src_table.mview_refresh_interval_seconds:
        dest_mv.mview_refresh_interval_seconds = src_table.mview_refresh_interval_seconds
    if hasattr(src_table, 'mview_enable_refresh') and src_table.mview_enable_refresh is not None:
        dest_mv.mview_enable_refresh = src_table.mview_enable_refresh

    try:
        dest_client.create_table(dest_mv)
        log(f"    ✅ Created materialized view: {src_table.table_id}")
        stats["mviews"] += 1
    except Exception as e:
        log(f"    ❌ Failed to create MV {src_table.table_id}: {e}")
        stats["errors"] += 1


# ─── PHASE 2: FK TABLE FIXES ─────────────────────────────────────────────────

def phase2_fk_fixes():
    """Copy tables that failed due to cross-project FK constraints."""
    log("=" * 60)
    log("PHASE 2: FK-Constraint Table Fixes")
    log("=" * 60)

    total_success = 0
    total_failed = 0

    for dataset_id, tables in FK_TABLES.items():
        log(f"\n📦 Dataset: {dataset_id} ({len(tables)} tables)")
        success = 0
        failed = 0

        for table_id in tables:
            src_ref = bigquery.TableReference(
                bigquery.DatasetReference(SOURCE_PROJECT, dataset_id), table_id
            )
            dest_ref = bigquery.TableReference(
                bigquery.DatasetReference(DEST_PROJECT, dataset_id), table_id
            )

            # Check if already exists with data
            try:
                existing = dest_client.get_table(dest_ref)
                if existing.num_rows and existing.num_rows > 0:
                    log(f"    ⏭️  Already exists: {table_id} ({existing.num_rows:,} rows)")
                    success += 1
                    continue
            except NotFound:
                pass

            # Get source schema
            try:
                src_table = client.get_table(src_ref)
            except NotFound:
                log(f"    ⚠️  Source table not found: {table_id}")
                failed += 1
                continue

            # Create dest table without FK constraints
            dest_table = bigquery.Table(dest_ref)
            dest_table.schema = src_table.schema
            dest_table.description = src_table.description
            dest_table.labels = src_table.labels or {}
            if src_table.clustering_fields:
                dest_table.clustering_fields = src_table.clustering_fields
            if src_table.time_partitioning:
                dest_table.time_partitioning = src_table.time_partitioning

            try:
                dest_client.create_table(dest_table)
                log(f"    ✅ Created schema: {table_id}")
            except Conflict:
                pass

            # Copy data via query
            log(f"    📋 Copying data: {table_id} ...")
            job_config = bigquery.QueryJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                destination=dest_ref,
            )
            query = f"SELECT * FROM `{SOURCE_PROJECT}.{dataset_id}.{table_id}`"
            try:
                query_job = dest_client.query(query, job_config=job_config)
                query_job.result(timeout=600)
                rows = dest_client.get_table(dest_ref).num_rows
                log(f"    ✅ Copied: {table_id} ({rows:,} rows)")
                success += 1
            except Exception as e:
                log(f"    ❌ Failed: {table_id}: {e}")
                failed += 1

        log(f"  Dataset {dataset_id}: {success} copied, {failed} failed")
        total_success += success
        total_failed += failed

    log("\n" + "=" * 60)
    log(f"✅ Phase 2 Done!")
    log(f"   Total copied: {total_success}")
    log(f"   Total failed: {total_failed}")
    log("=" * 60)


# ─── PHASE 3: RECREATE ALL VIEWS ──────────────────────────────────────────────

def phase3_views():
    """Recreate all views across all datasets (drop existing, recreate)."""
    log("=" * 60)
    log("PHASE 3: Recreate All Views")
    log("=" * 60)

    datasets = list(client.list_datasets())
    total_created = 0
    total_failed = 0

    for src_ds_item in datasets:
        src_dataset = client.get_dataset(src_ds_item.reference)
        ds_id = src_dataset.dataset_id

        tables = list(client.list_tables(src_ds_item.reference))
        views = [t for t in tables if client.get_table(t.reference).table_type == "VIEW"]

        if not views:
            continue

        log(f"\n📦 Dataset: {ds_id} ({len(views)} views)")
        created = 0
        failed = 0

        for tbl_item in views:
            src_table = client.get_table(tbl_item.reference)
            dest_ref = bigquery.TableReference(
                bigquery.DatasetReference(DEST_PROJECT, ds_id), src_table.table_id
            )

            # Drop existing (may be broken)
            try:
                dest_client.delete_table(dest_ref)
            except NotFound:
                pass

            view_query = src_table.view_query
            if not view_query:
                continue

            rewritten = view_query.replace(SOURCE_PROJECT, DEST_PROJECT)
            dest_view = bigquery.Table(dest_ref)
            dest_view.view_query = rewritten
            dest_view.description = src_table.description
            dest_view.labels = src_table.labels or {}

            try:
                dest_client.create_table(dest_view)
                log(f"    ✅ {src_table.table_id}")
                created += 1
            except Exception as e:
                err_msg = str(e)[:100]
                log(f"    ❌ {src_table.table_id}: {err_msg}")
                failed += 1

        total_created += created
        total_failed += failed

    log("\n" + "=" * 60)
    log(f"✅ Phase 3 Done!")
    log(f"   Views created: {total_created}")
    log(f"   Views failed:  {total_failed}")
    log("=" * 60)


# ─── PHASE 4: MSREAD_PRODUCTS ──────────────────────────────────────────────────

def phase4_products():
    """Copy msread_products tables and views (external tables skipped)."""
    log("=" * 60)
    log("PHASE 4: msread_products")
    log("=" * 60)

    dataset_id = "msread_products"
    src_dataset = client.get_dataset(f"{SOURCE_PROJECT}.{dataset_id}")
    tables = list(client.list_tables(f"{SOURCE_PROJECT}.{dataset_id}"))

    # Ensure dataset exists
    dest_ref = f"{DEST_PROJECT}.{dataset_id}"
    dest_ds = bigquery.Dataset(dest_ref)
    dest_ds.description = src_dataset.description
    dest_ds.labels = src_dataset.labels or {}
    dest_ds.location = src_dataset.location or "asia-southeast1"
    try:
        dest_client.create_dataset(dest_ds, exists_ok=True)
    except Conflict:
        pass

    stats = {"tables": 0, "views": 0, "external": 0}

    for tbl_item in tables:
        src_table = client.get_table(tbl_item.reference)

        if src_table.table_type == "TABLE":
            _copy_table(src_table, dataset_id, stats)
        elif src_table.table_type == "VIEW":
            _recreate_view(src_table, dataset_id, stats)
        elif src_table.table_type == "EXTERNAL":
            log(f"    ⚠️  External (Google Sheet): {src_table.table_id} — skipped")
            stats["external"] += 1

    log(f"\n✅ Phase 4 Done! {dataset_id}:")
    log(f"   Tables:    {stats['tables']}")
    log(f"   Views:     {stats['views']}")
    log(f"   External:  {stats['external']} (skipped)")
    log("=" * 60)


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="BigQuery Project Cloner — All-in-One")
    parser.add_argument("--all", action="store_true", help="Run all phases (1-4)")
    parser.add_argument("--clone", action="store_true", help="Phase 1: Full project clone")
    parser.add_argument("--fk", action="store_true", help="Phase 2: Fix FK-constraint tables")
    parser.add_argument("--views", action="store_true", help="Phase 3: Recreate all views")
    parser.add_argument("--products", action="store_true", help="Phase 4: msread_products")
    args = parser.parse_args()

    if not any([args.all, args.clone, args.fk, args.views, args.products]):
        parser.print_help()
        print("\nExample: python bq_clone_all.py --all")
        return

    log(f"🚀 BigQuery Project Cloner")
    log(f"   {SOURCE_PROJECT} → {DEST_PROJECT}")
    log(f"   Auth: {'Secret Manager' if platform.system() != 'Windows' else 'JSON file'}")
    log("")

    if args.all or args.clone:
        phase1_clone()

    if args.all or args.fk:
        phase2_fk_fixes()

    if args.all or args.views:
        phase3_views()

    if args.all or args.products:
        phase4_products()

    log("\n🏁 All requested phases complete!")


if __name__ == "__main__":
    main()