# Deploying the demo to AWS App Runner

Status: **not deployed yet.** These are the steps; nothing here has been run
against an AWS account from this repository.

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
#   health check: HTTP /health
#   env: PLANNER=rule   (no secrets needed for the deterministic planner)
```

If the LLM planner is enabled (`PLANNER=llm`), store `LLM_API_KEY` in AWS
Secrets Manager and reference it from the service configuration. Never commit
keys or put them in the image.

After deployment, run the same end-to-end check used in CI:

```bash
./scripts/smoke_test.sh https://<service-id>.<region>.awsapprunner.com
```
