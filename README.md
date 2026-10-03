# caremetrics-source

Synthetic healthcare operational database (PostgreSQL) for the CareMetrics analytics engineering portfolio project.

This repository is the **source system** in the pipeline:

operational Postgres -> Airbyte -> BigQuery raw -> dbt -> Looker Studio

All data is synthetic.
It contains no real PHI, and all payer names are fictional.

Full setup and usage documentation will be added as the project is built.

What it does: It anchors the repo and states what this repo is for, and that it holds no PHI, from the first commit. The full README comes in step 22.

Commands (from the project root, care-metrics-source):

git init -b main
git status
