"""
Copy fivetran tables that failed due to FOREIGN KEY constraints.
These tables are copied WITHOUT their FK constraints (data only).
"""

from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core.exceptions import Conflict, NotFound, BadRequest
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

# Tables in fivetran_auri that failed due to FK constraints
FK_TABLES = [
    "abandoned_checkout",
    "abandoned_checkout_line",
    "abandoned_checkout_shipping_line",
    "abandoned_checkout_url_tag",
    "app",
    "collection_product",
    "customer",
    "customer_address",
    "customer_tag",
    "customer_visit",
    "discount_application",
    "draft_order",
    "draft_order_line",
    "draft_order_note_attribute",
    "draft_order_tag",
    "fulfillment",
    "fulfillment_order",
    "fulfillment_order_line",
    "gift_card",
    "inventory_level",
    "inventory_quantity",
    "media_image",
    "media_source",
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
    "product_tag",
    "product_variant",
    "product_variant_media",
    "refund",
    "tender_transaction",
    "video",
]

DATASET_ID = "fivetran_auri"


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
        dest_client.get_table(dest_ref)
        log(f"    ⏭️  Table already exists: {table_id}")
        return True
    except NotFound:
        pass

    # Get source table schema
    src_table = client.get_table(src_ref)

    # Create destination table with same schema but WITHOUT foreign keys
    dest_table = bigquery.Table(dest_ref)
    dest_table.schema = src_table.schema

    # Copy description and labels
    dest_table.description = src_table.description
    dest_table.labels = src_table.labels or {}

    # Do NOT copy: clustering, time_partitioning, foreign_keys, table_constraints
    # But DO copy: clustering and partitioning (those are fine across projects)
    if src_table.clustering_fields:
        dest_table.clustering_fields = src_table.clustering_fields
    if src_table.time_partitioning:
        dest_table.time_partitioning = src_table.time_partitioning

    # Create the empty table first (without FK constraints)
    try:
        dest_table = dest_client.create_table(dest_table)
        log(f"    ✅ Created table schema: {table_id}")
    except Conflict:
        log(f"    ⏭️  Table already exists: {table_id}")
        dest_table = dest_client.get_table(dest_ref)

    # Now copy the data using a query job
    log(f"    📋 Copying data: {table_id} ...")
    job_config = bigquery.QueryJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        destination=dest_ref,
    )

    query = f"SELECT * FROM `{SOURCE_PROJECT}.{dataset_id}.{table_id}`"
    try:
        query_job = dest_client.query(query, job_config=job_config)
        query_job.result(timeout=600)  # Wait up to 10 min per table
        log(f"    ✅ Copied data: {table_id}")
        return True
    except Exception as e:
        log(f"    ❌ Failed to copy data for {table_id}: {e}")
        return False


def main():
    log(f"🚀 Copying {len(FK_TABLES)} FK-constraint tables from {DATASET_ID}")
    log(f"   Source: {SOURCE_PROJECT} → Dest: {DEST_PROJECT}")
    log("")

    success = 0
    failed = 0
    skipped = 0

    for table_id in FK_TABLES:
        result = copy_table_no_fk(DATASET_ID, table_id)
        if result is True:
            # Check if it was actually copied or just already existed
            dest_ref = bigquery.TableReference(
                bigquery.DatasetReference(DEST_PROJECT, DATASET_ID),
                table_id,
            )
            try:
                t = dest_client.get_table(dest_ref)
                if t.num_rows > 0:
                    success += 1
                else:
                    # Check if it was a skip (already existed from previous run)
                    skipped += 1
            except:
                success += 1
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