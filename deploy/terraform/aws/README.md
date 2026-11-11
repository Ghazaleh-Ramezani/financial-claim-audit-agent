# AWS deployment (Terraform)

Infrastructure for running the claim-audit service on AWS: the API on ECS Fargate
behind an Application Load Balancer, the claim store on RDS PostgreSQL 16, and an
event-driven ingestion pipeline that turns an S3 upload into validated claims.

```
                         ┌──────────────────── VPC (2 AZs) ─────────────────────────────┐
 client ──HTTP(S)──▶ ALB ─┼─▶ ECS Fargate: API tasks (public subnets, SG: ALB only)      │
                         │        │  /ask /claims /restatements /metrics                  │
                         │        ▼                                                       │
                         │   RDS PostgreSQL 16 (private subnets, SG: API + ingest only,   │
                         │   TLS enforced, encrypted, password managed by Secrets Manager)│
                         │        ▲                                                       │
 aws s3 cp filing.json   │        │                                                       │
   s3://…/incoming/ ──▶ S3 ──▶ EventBridge ──▶ Step Functions ──▶ Lambda (private subnets)│
   (versioned, TLS-only)  "Object Created"      Validate ─▶ Ingest ─▶ ✓                   │
                         │                         │          │ DependencyNotReady: retry  │
                         │                         └──────────┴─▶ Quarantine ─▶ rejected/ │
                         └──────────────────────────────────────────────────────────────┘
 CloudWatch: logs for every component, Container Insights, X-Ray traces for the pipeline,
 alarms → SNS (5xx, unhealthy targets, p95 latency, failed ingestions, Lambda throttling,
 RDS CPU and storage). Autoscaling on CPU and on requests per task.
```

| File | What it defines |
|---|---|
| `network.tf` | VPC, public/private subnets, S3 gateway and Secrets Manager interface endpoints, security groups |
| `data.tf` | ECR repositories, RDS PostgreSQL 16, the filings bucket |
| `api.tf` | ALB, ECS cluster, task definition, service with circuit-breaker rollback, autoscaling |
| `ingest.tf` | Ingest Lambda (container image), Step Functions state machine, EventBridge rule |
| `iam.tf` | One least-privilege role per component |
| `monitoring.tf` | SNS topic and CloudWatch alarms |

## Design decisions

* **Secrets.** RDS generates and rotates the master password in Secrets Manager
  (`manage_master_user_password`); ECS injects it as `DB_PASSWORD` and the Lambda reads it
  at cold start. It never appears in Terraform variables, state outputs or images.
* **Ingestion is a state machine, not a script.** Validation failures are caught and the
  object is copied to `rejected/` with its reason; the execution then fails, which is what
  the `ingest-executions-failed` alarm watches. An amendment that arrives before its
  original raises `DependencyNotReady` and is retried with backoff instead of rejected.
* **Idempotent at every layer.** EventBridge and Step Functions deliver at least once;
  ingestion deduplicates on the document's SHA-256 and rejects a changed document under an
  existing accession, so redelivery cannot create duplicate or altered claims.
* **Lineage.** Each claim's source URI pins the S3 object version
  (`s3://bucket/incoming/x.json?versionId=…`), so an answer traces to the exact bytes.
* **Bounded database load.** The Lambda's reserved concurrency caps its connections to RDS.
* **No NAT gateway.** Tasks pull images through a public IP (their security group admits
  only the load balancer); private subnets reach S3 and Secrets Manager through endpoints.
  This keeps a demo deployment inexpensive. For production, move tasks to private subnets
  behind NAT or add ECR and CloudWatch interface endpoints.

## Deploy

Prerequisites: an AWS account, the AWS CLI with credentials, Terraform ≥ 1.6, Docker.

```bash
cd deploy/terraform/aws
terraform init

# 1. Create the registries first: the Lambda and the ECS service need images to exist.
terraform apply -target=aws_ecr_repository.api -target=aws_ecr_repository.ingest

# 2. Build and push both images (from the repository root).
REGION=ca-central-1
API=$(terraform output -raw ecr_api_repository)
INGEST=$(terraform output -raw ecr_ingest_repository)
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${API%%/*}
(cd ../../.. && docker build -t $API:latest . && docker push $API:latest)
(cd ../../.. && docker build -f deploy/lambda/Dockerfile -t $INGEST:latest . && docker push $INGEST:latest)

# 3. Everything else.
terraform apply -var alarm_email=you@example.com

# 4. Ingest the synthetic filings through the pipeline and run the end-to-end check.
BUCKET=$(terraform output -raw filings_bucket)
aws s3 cp ../../../fixtures/filings s3://$BUCKET/incoming/ --recursive
sleep 90   # the pipeline is asynchronous; amendments may wait for their originals
sh ../../../scripts/smoke_test.sh $(terraform output -raw api_url)
```

Upload order does not matter: amendments uploaded before their originals are retried.
`fixtures/filings/acme_10k_fy2025_redownload.json` is the same document as
`acme_10k_fy2025.json` and is recorded as a duplicate. Uploading a file from
`fixtures/invalid/` exercises the quarantine path.

**Cost.** With the defaults (one 0.5 vCPU task, `db.t4g.micro`, one ALB, two Secrets
Manager endpoints) the stack costs on the order of USD 2–4 per day. Remove it with
`terraform destroy` when you are done; with `db_deletion_protection = false` (the default)
the database, bucket and registries are deleted with it.

## Checks in CI

The `terraform` job in `.github/workflows/tests.yml` runs `terraform fmt -check`,
`terraform init -backend=false`, `terraform validate` and `tflint` with the AWS ruleset on
every push. The `docker` job builds the Lambda image and invokes the handler through the
Lambda Runtime Interface Emulator; `tests/test_aws_ingest.py` runs the validate, ingest,
quarantine and secret-loading steps against moto's S3 and Secrets Manager.
