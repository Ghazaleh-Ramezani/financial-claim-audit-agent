# Least privilege: each role can touch only the resources its step needs.

data "aws_iam_policy_document" "assume_ecs_tasks" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: pull the image, write logs, read the DB secret into the container.
resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = compact([local.db_secret_arn, var.llm_api_key_secret_arn])
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "read-runtime-secrets"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution_secrets.json
}

# Task role: the API is read-only against its own database and calls no AWS API.
resource "aws_iam_role" "ecs_task" {
  name               = "${local.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
}

# ---------------------------------------------------------------- ingest Lambda

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingest_lambda" {
  name               = "${local.name}-ingest-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role_policy_attachment" "ingest_lambda_vpc" {
  role       = aws_iam_role.ingest_lambda.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "ingest_lambda_xray" {
  role       = aws_iam_role.ingest_lambda.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

data "aws_iam_policy_document" "ingest_lambda" {
  statement {
    sid       = "ReadIncoming"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.filings.arn}/incoming/*"]
  }

  statement {
    sid       = "WriteRejected"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.filings.arn}/rejected/*"]
  }

  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.db_secret_arn]
  }
}

resource "aws_iam_role_policy" "ingest_lambda" {
  name   = "ingest-filings"
  role   = aws_iam_role.ingest_lambda.id
  policy = data.aws_iam_policy_document.ingest_lambda.json
}

# ---------------------------------------------------------------- Step Functions

data "aws_iam_policy_document" "assume_states" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingest_sfn" {
  name               = "${local.name}-ingest-sfn"
  assume_role_policy = data.aws_iam_policy_document.assume_states.json
}

data "aws_iam_policy_document" "ingest_sfn" {
  statement {
    sid       = "InvokeIngestLambda"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.ingest.arn, "${aws_lambda_function.ingest.arn}:*"]
  }

  statement {
    sid = "DeliverLogs" # CloudWatch Logs delivery for Step Functions requires these on "*"
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "Tracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ingest_sfn" {
  name   = "run-ingest"
  role   = aws_iam_role.ingest_sfn.id
  policy = data.aws_iam_policy_document.ingest_sfn.json
}

# ---------------------------------------------------------------- EventBridge

data "aws_iam_policy_document" "assume_events" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events_start_ingest" {
  name               = "${local.name}-events-start-ingest"
  assume_role_policy = data.aws_iam_policy_document.assume_events.json
}

data "aws_iam_policy_document" "events_start_ingest" {
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.ingest.arn]
  }
}

resource "aws_iam_role_policy" "events_start_ingest" {
  name   = "start-ingest"
  role   = aws_iam_role.events_start_ingest.id
  policy = data.aws_iam_policy_document.events_start_ingest.json
}
