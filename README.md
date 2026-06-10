# BigQuery Project Cloner

Clones all datasets, tables, and views from `msr-msia-sales-analysis` to `msr-msia-sales-analysis-clone`.

## Overview

| | Details |
|---|---|
| **Source Project** | `msr-msia-sales-analysis` |
| **Destination Project** | `msr-msia-sales-analysis-clone` |
| **Service Account** | `myai-996@msr-msia-sales-analysis.iam.gserviceaccount.com` |
| **GCP VM** | `bq-clone` (zone: `asia-southeast1-a`) |
| **GitHub Repo** | https://github.com/msritmsread-collab/bq-clone |
| **Schedule** | Daily at 2:00 PM (cron) |

## Phases

| Phase | Flag | Description |
|-------|------|-------------|
| 1 | `--clone` | Full project clone — all datasets, tables, views, materialized views |
| 2 | `--fk` | Fix FK-constraint tables — copies 88 tables from `fivetran_auri` (44) and `fivetran_msread` (44) without foreign key constraints |
| 3 | `--views` | Recreate all views — drops and recreates every view across all datasets with rewritten project references |
| 4 | `--products` | `msread_products` — copies tables and views, skips external (Google Sheet) tables |

Run all phases: `python3 bq_clone_all.py --all`

## Authentication

The script auto-detects the environment and tries 3 auth methods in order:

1. **Secret Manager** — pulls the SA key from `connector-bq-service-account` secret (for VMs with proper scopes)
2. **VM Default Credentials** — uses the GCP VM's built-in service account (for VMs with BigQuery access)
3. **JSON Key File** — reads `bigquery_credentials.json` from the script directory (local development / VMs without scopes)

If no method works, the script exits with instructions.

## VM Setup

### Initial Setup

```bash
# SSH into VM
gcloud compute ssh bq-clone --zone=asia-southeast1-a

# Install dependencies
sudo apt-get update && sudo apt-get install -y git
python3 -m ensurepip --user
python3 -m pip install --user --break-system-packages google-cloud-bigquery google-cloud-secret-manager

# Clone repo
git clone https://github.com/msritmsread-collab/bq-clone.git
cd bq-clone

# Upload credentials (run from Windows, not inside VM)
gcloud compute scp bigquery_credentials.json msread_bigquery@bq-clone:~/bq-clone/ --zone=asia-southeast1-a
```

### Running the Script

```bash
cd ~/bq-clone
PYTHONIOENCODING=utf-8 python3 bq_clone_all.py --all
```

### Individual Phases

```bash
python3 bq_clone_all.py --clone     # Phase 1 only
python3 bq_clone_all.py --fk        # Phase 2 only
python3 bq_clone_all.py --views     # Phase 3 only
python3 bq_clone_all.py --products  # Phase 4 only
```

### Daily Cron Job (2 PM)

```bash
# Add cron job
(crontab -l 2>/dev/null; echo "0 14 * * * cd /home/msread_bigquery/bq-clone && PYTHONIOENCODING=utf-8 /usr/bin/python3 bq_clone_all.py --all >> /home/msread_bigquery/bq-clone/cron.log 2>&1") | crontab -

# Verify
crontab -l

# View logs
tail -50 ~/bq-clone/cron.log
```

### Update Script from GitHub

```bash
cd ~/bq-clone
git pull
```

## Known Limitations

| Limitation | Details |
|---|---|
| **External Tables** | ~251 Google Sheet linked tables cannot be cloned via API. Must be re-linked manually in GCP Console. |
| **FK Constraints** | Cross-project foreign keys are not allowed. Phase 2 copies these tables without FK constraints. |
| **View Dependencies** | Views referencing tables in datasets not yet created fail on first pass. Re-running `--views` (Phase 3) fixes most. Some with circular dependencies or referencing missing datasets (`fivetran_msread_sg`, `fivetran_auri_sg`) will remain broken. |
| **Unicode on Windows** | Run with `PYTHONIOENCODING=utf-8` to avoid `charmap` codec errors with emoji in output. |

## Files

| File | Purpose |
|---|---|
| `bq_clone_all.py` | **Combined script** — all 4 phases with CLI flags. Use this for VM/cron. |
| `bq_clone_project.py` | Standalone Phase 1 — full project clone |
| `bq_clone_fivetran_fk.py` | Standalone Phase 2 — FK fix for `fivetran_auri` only |
| `bq_clone_fivetran_msread_fk.py` | Standalone Phase 2 — FK fix for `fivetran_msread` only |
| `bq_clone_report_msread.py` | Standalone Phase 3 — `Report_MSREAD` views only |
| `bq_clone_msread_products.py` | Standalone Phase 4 — `msread_products` only |
| `.gitignore` | Excludes credential JSON files and runtime state |

## Architecture

```
Source Project                          Destination Project
┌──────────────────────┐               ┌──────────────────────┐
│ msr-msia-sales-analysis │    ────►    │ msr-msia-sales-analysis-clone │
│                        │               │                        │
│  82 datasets           │   Phase 1     │  82 datasets           │
│  1,455 tables          │   ────►       │  1,455 tables          │
│  307 views             │   Phase 3     │  ~290 views ✅         │
│  ~251 external tables  │   (skipped)   │  ~17 views ❌ (circular)│
│                        │               │                        │
│  fivetran_auri (44)    │   Phase 2     │  44 tables (no FK) ✅  │
│  fivetran_msread (44)  │   ────►       │  44 tables (no FK) ✅  │
│                        │               │                        │
│  msread_products       │   Phase 4     │  4 tables ✅           │
│                        │   ────►       │  2 views ✅            │
│                        │               │  172 external (skipped)│
└──────────────────────┘               └──────────────────────┘
```

## Troubleshooting

### `ModuleNotFoundError: No module named 'google.cloud'`
```bash
python3 -m pip install --user --break-system-packages google-cloud-bigquery google-cloud-secret-manager
```

### `FileNotFoundError: bigquery_credentials.json`
Upload the credentials file to the VM:
```bash
gcloud compute scp bigquery_credentials.json msread_bigquery@bq-clone:~/bq-clone/ --zone=asia-southeast1-a
```

### `ACCESS_TOKEN_SCOPE_INSUFFICIENT` (Secret Manager)
The VM doesn't have Secret Manager scope. The script falls back to JSON file auth automatically. No action needed if `bigquery_credentials.json` is present.

### Views still failing after `--views`
Some views have circular dependencies or reference datasets that don't exist in the clone project (`fivetran_msread_sg`, `fivetran_auri_sg`). These cannot be resolved without also cloning those datasets.

### Check cron is running
```bash
grep CRON /var/log/syslog | tail -5
tail -50 ~/bq-clone/cron.log
```