"""
Copy fivetran_msread tables that failed due to FOREIGN KEY constraints.
These tables are copied WITHOUT their FK constraints (data only).
Skips tables that already exist in the destination.
"""

from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core.exceptions import Conflict, NotFound
import time

# ─── CONFIG ────────────────────────────────────────────────────────────────────
SOURCE_PROJECT = "msr-msia-sales-analysis"
DEST_PROJECT   = "msr-msia-sales-analysis-clone"

CRED_PATH = "bigquery_credentials.json"
credentials = service_account.Credentials.from_service_account_file(
    CRED_PATH, scopes=["https://www.googleapis.com/auth/bigquery"]
)
client = bigquery.Client(project=SOURCE_PROJECT, credentials=credentials)
dest_client = bigquery.Client(project=DEST_PROJECT, credentials=credentials)

# Tables in fivetran_msread that had FK constraint failures
# (excludes ones already copied via fivetran_auri script)
FK_TABLES = [
    "collection_product",
    "company_location_catalog",
    "customer",
    "customer_address",
    "customer_tag",
    "customer_visit",
    "discount_application",
    "discount_customer_buys_collection",
    "discount_customer_buys_product",
    "discount_customer_gets_collection",
    "discount_customer_gets_product",
    "discount_customer_segment_selection",
    "discount_customer_selection",
    "fulfillment",
    "fulfillment_event",
    "fulfillment_order",
    "fulfillment_order_line",
    "inventory_level",
    "inventory_quantity",
    "order",
    "order_adjustment",
    "order_discount_code",
    "order_line",
    "order_line_refund",
    "order_note_attribute",
    "order_risk_assessment",
    "order_risk_fact",
    "order_risk_summary",
    "order_shipping_line",
    "order_tag",
    "order_url_tag",
    "product_media",
    "product_option",
    "product_option_value",
    "product_publication",
    "product_tag",
    "product_variant",
    "product_variant_media",
    "publication",
    "refund",
    "return",
    "sale",
    "sales_agreement",
    "video",
]

DATASET_ID = "fivetran_msread"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def copy_table_no_fk(dataset_id: str, table_id: str):
    """Copy a table from source to dest, skipping FK constraints."""
    src_ref = bigquery.TableReference(
        bigquery.DatasetReference(SOURCE_PROJECT, dataset_id),
        table_id,
    )
    dest_ref = bigquery.TableReference(
        bigquery.DatasetReference(DEST_PROJECT, dataset_id),
        table_id,
    )

    # Skip if already exists
    try:
        existing = dest_client.get_table(dest_ref)
        if existing.num_rows > 0:
            log(f"    ⏭️  Table already exists with data: {table_id} ({existing.num_rows} rows)")
            return "skip"
        else:
            # Table exists but empty — still need to load data
            log(f"    📋 Table exists but empty, loading data: {table_id}")
    except NotFound:
        # Need to create table + load data
        src_table = client.get_table(src_ref)

        dest_table = bigquery.Table(dest_ref)
        dest_table.schema = src_table.schema
        dest_table.description = src_table.description
        dest_table.labels = src_table.labels or {}

        if src_table.clustering_fields:
            dest_table.clustering_fields = src_table.clustering_fields
        if src_table.time_partitioning:
            dest_table.time_partitioning = src_table.time_partitioning

        try:
            dest_table = dest_client.create_table(dest_table)
            log(f"    ✅ Created table schema: {table_id}")
        except Conflict:
            log(f"    ⏭️  Table created by another process: {table_id}")

    # Copy data using query
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
        log(f"    ✅ Copied data: {table_id} ({rows:,} rows)")
        return "success"
    except Exception as e:
        log(f"    ❌ Failed to copy data for {table_id}: {e}")
        return "fail"


def main():
    log(f"🚀 Copying {len(FK_TABLES)} FK-constraint tables from {DATASET_ID}")
    log(f"   Source: {SOURCE_PROJECT} → Dest: {DEST_PROJECT}")
    log("")

    success = 0
    failed = 0
    skipped = 0

    for table_id in FK_TABLES:
        result = copy_table_no_fk(DATASET_ID, table_id)
        if result == "success":
            success += 1
        elif result == "skip":
            skipped += 1
        else:
            failed += 1

    log("")
    log("=" * 60)
    log(f"✅ Done!")
    log(f"   Copied:  {success}")
    log(f"   Skipped: {skipped}")
    log(f"   Failed:  {failed}")
    log("=" * 60)


if __name__ == "__main__":
    main()