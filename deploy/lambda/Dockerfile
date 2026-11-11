# Ingest Lambda (app/aws_ingest.py) as a container image.
# Build from the repository root:  docker build -f deploy/lambda/Dockerfile -t claim-audit-ingest .
FROM public.ecr.aws/lambda/python:3.12

# Only what ingestion needs; boto3 is part of the Lambda base image.
RUN pip install --no-cache-dir "sqlalchemy>=2.0.30,<3" "psycopg[binary]>=3.2,<4"

COPY app/__init__.py app/aws_ingest.py app/database.py app/ingest.py ${LAMBDA_TASK_ROOT}/app/

CMD ["app.aws_ingest.handler"]
