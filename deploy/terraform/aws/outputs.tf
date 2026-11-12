output "api_url" {
  description = "Base URL of the API."
  value       = var.certificate_arn == "" ? "http://${aws_lb.api.dns_name}" : "https://${aws_lb.api.dns_name}"
}

output "ecr_api_repository" {
  description = "Push the API image here."
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_ingest_repository" {
  description = "Push the ingest Lambda image here."
  value       = aws_ecr_repository.ingest.repository_url
}

output "filings_bucket" {
  description = "Upload filings to s3://<bucket>/incoming/ to ingest them."
  value       = aws_s3_bucket.filings.bucket
}

output "ingest_state_machine_arn" {
  description = "Ingestion state machine."
  value       = aws_sfn_state_machine.ingest.arn
}

output "db_endpoint" {
  description = "RDS endpoint (private)."
  value       = aws_db_instance.claims.address
}

output "db_secret_arn" {
  description = "Secrets Manager secret with the RDS master credentials."
  value       = local.db_secret_arn
}

output "alarm_topic_arn" {
  description = "SNS topic receiving alarm notifications."
  value       = aws_sns_topic.alarms.arn
}
