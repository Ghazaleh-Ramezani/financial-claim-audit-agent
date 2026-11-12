# S3 upload -> EventBridge -> Step Functions (validate -> ingest | quarantine) -> Lambda.
# The Lambda is app/aws_ingest.py packaged by deploy/lambda/Dockerfile.

resource "aws_cloudwatch_log_group" "ingest_lambda" {
  name              = "/aws/lambda/${local.name}-ingest"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "ingest" {
  function_name = "${local.name}-ingest"
  role          = aws_iam_role.ingest_lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.ingest.repository_url}:${var.image_tag}"
  architectures = ["x86_64"]
  timeout       = 60
  memory_size   = 512

  # Bounds the number of concurrent connections the pipeline can open to RDS.
  reserved_concurrent_executions = var.ingest_concurrency

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.ingest.id]
  }

  environment {
    variables = {
      DB_HOST       = aws_db_instance.claims.address
      DB_PORT       = tostring(aws_db_instance.claims.port)
      DB_NAME       = aws_db_instance.claims.db_name
      DB_SSLMODE    = "require"
      DB_SECRET_ARN = local.db_secret_arn
    }
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [aws_cloudwatch_log_group.ingest_lambda]
}

resource "aws_cloudwatch_log_group" "ingest_sfn" {
  name              = "/aws/vendedlogs/states/${local.name}-ingest"
  retention_in_days = var.log_retention_days
}

locals {
  lambda_transient_errors = [
    "Lambda.ServiceException",
    "Lambda.AWSLambdaException",
    "Lambda.SdkClientException",
    "Lambda.TooManyRequestsException",
  ]

  ingest_definition = {
    Comment = "Validate a filing uploaded to incoming/, write it to the claim store, or quarantine it."
    StartAt = "Validate"
    States = {
      Validate = {
        Type     = "Task"
        Resource = "arn:${local.partition}:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.ingest.arn
          Payload      = { action = "validate", "input.$" = "$" }
        }
        ResultSelector = { "summary.$" = "$.Payload" }
        ResultPath     = "$.validated"
        Retry = [{
          ErrorEquals     = local.lambda_transient_errors
          IntervalSeconds = 2
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{ ErrorEquals = ["ValidationError"], ResultPath = "$.error", Next = "Quarantine" }]
        Next  = "Ingest"
      }

      Ingest = {
        Type     = "Task"
        Resource = "arn:${local.partition}:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.ingest.arn
          Payload = {
            action = "ingest"
            input = {
              "bucket.$" = "$.validated.summary.bucket"
              "key.$"    = "$.validated.summary.key"
            }
          }
        }
        ResultSelector = { "result.$" = "$.Payload" }
        ResultPath     = "$.ingested"
        Retry = [
          {
            # An amendment processed before the filing it amends: wait for the original.
            ErrorEquals     = ["DependencyNotReady"]
            IntervalSeconds = 15
            MaxAttempts     = 6
            BackoffRate     = 2
          },
          {
            ErrorEquals     = concat(local.lambda_transient_errors, ["OperationalError"])
            IntervalSeconds = 5
            MaxAttempts     = 3
            BackoffRate     = 2
          },
        ]
        Catch = [{ ErrorEquals = ["ValidationError", "DependencyNotReady"], ResultPath = "$.error", Next = "Quarantine" }]
        Next  = "Ingested"
      }

      Quarantine = {
        Type     = "Task"
        Resource = "arn:${local.partition}:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.ingest.arn
          Payload = {
            action = "quarantine"
            input = {
              "bucket.$" = "$.detail.bucket.name"
              "key.$"    = "$.detail.object.key"
              "error.$"  = "$.error"
            }
          }
        }
        ResultPath = "$.quarantined"
        Retry = [{
          ErrorEquals     = local.lambda_transient_errors
          IntervalSeconds = 2
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Next = "Rejected"
      }

      # A failed execution is the signal: the IngestExecutionsFailed alarm fires on it.
      Rejected = {
        Type  = "Fail"
        Error = "FilingRejected"
        Cause = "The document failed validation; it was copied to rejected/ with the reason."
      }

      Ingested = { Type = "Succeed" }
    }
  }
}

resource "aws_sfn_state_machine" "ingest" {
  name     = "${local.name}-ingest"
  role_arn = aws_iam_role.ingest_sfn.arn
  type     = "STANDARD"

  definition = jsonencode(local.ingest_definition)

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.ingest_sfn.arn}:*"
    include_execution_data = false
    level                  = "ERROR"
  }

  tracing_configuration {
    enabled = true
  }
}

resource "aws_cloudwatch_event_rule" "filing_uploaded" {
  name        = "${local.name}-filing-uploaded"
  description = "A filing was uploaded to incoming/ in the filings bucket"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.filings.bucket] }
      object = { key = [{ prefix = "incoming/" }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "start_ingest" {
  rule     = aws_cloudwatch_event_rule.filing_uploaded.name
  arn      = aws_sfn_state_machine.ingest.arn
  role_arn = aws_iam_role.events_start_ingest.arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }
}
