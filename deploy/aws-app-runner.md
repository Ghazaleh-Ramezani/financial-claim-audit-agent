# Deploying the demo to AWS App Runner

Status: documented. The container image these steps deploy is the one CI builds
from the `Dockerfile` and checks end to end with `scripts/smoke_test.sh`; the AWS
service itself is created from the steps below.

The container defaults to SQLite inside the container and ingests the synthetic
fixtures on start (`INGEST_ON_START=1`), so a single App Runner service is
enough for a read-only demo. For PostgreSQL, set `DATABASE_URL` to an RDS
instance reachable through an App Runner VPC connector.

```bash
AWS_REGION=ca-central-1
REPO=financial-claim-audit-agent
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO

aws ecr create-repository --repository-name $REPO --region $AWS_REGION
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin ${ECR%/*}
docker build -t $ECR:latest . && docker push $ECR:latest

# Create the service in the console (App Runner -> Create service -> Container registry -> ECR)
# or with `aws apprunner create-service`, using:
#   port: 8000
#   health check: HTTP /ready   (503 until the claim store is loaded, so no traffic before ingestion)
#   env: PLANNER=rule   (no secrets needed for the deterministic planner)
```

If the LLM planner is enabled (`PLANNER=llm`), store `LLM_API_KEY` in AWS
Secrets Manager and reference it from the service configuration. Never commit
keys or put them in the image.

After deployment, run the same end-to-end check used in CI:

```bash
sh scripts/smoke_test.sh https://<service-id>.<region>.awsapprunner.com
```

## Operating it

* **Logs.** The service writes one JSON line per request (`event: http_request`,
  with `request_id`, path, status and latency) and one per agent run (with
  `trace_id`); App Runner forwards stdout to CloudWatch Logs, where both can be
  queried by id. Callers can pass their own `X-Request-ID`.
* **Metrics and alerts.** `/metrics` is in Prometheus format. Scrape it with a
  Prometheus-compatible collector and load `deploy/prometheus/alerts.yml`; the
  rules apply unchanged. Start with the abstention-rate and unregistered-tool
  alerts: the first catches regressions and poisoned documents that make the agent
  withhold answers, the second a model trying to call a tool that does not exist.
* **Scaling.** One uvicorn worker per instance; scale by instance count. The load
  test (`evaluations/results_load.md`) shows where one worker levels off.
