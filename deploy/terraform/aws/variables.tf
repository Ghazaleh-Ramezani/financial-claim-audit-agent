variable "region" {
  description = "AWS region."
  type        = string
  default     = "ca-central-1"
}

variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "claim-audit"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.project))
    error_message = "project must be 3-21 lowercase letters, digits or hyphens."
  }
}

variable "image_tag" {
  description = "Tag of the API and ingest images in ECR (CI pushes the git SHA)."
  type        = string
  default     = "latest"
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "allowed_ingress_cidrs" {
  description = "CIDR blocks allowed to reach the load balancer."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS. Empty = HTTP only (demo)."
  type        = string
  default     = ""
}

variable "api_cpu" {
  description = "Fargate CPU units per API task."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Fargate memory (MiB) per API task."
  type        = number
  default     = 1024
}

variable "api_min_count" {
  description = "Minimum number of API tasks."
  type        = number
  default     = 1
}

variable "api_max_count" {
  description = "Maximum number of API tasks under autoscaling. One uvicorn worker per task; scale by tasks."
  type        = number
  default     = 4
}

variable "planner" {
  description = "Planner for /ask: rule (deterministic, no secrets) or llm."
  type        = string
  default     = "rule"

  validation {
    condition     = contains(["rule", "llm"], var.planner)
    error_message = "planner must be rule or llm."
  }
}

variable "llm_api_key_secret_arn" {
  description = "Secrets Manager ARN holding the LLM API key (only when planner = llm)."
  type        = string
  default     = ""
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_deletion_protection" {
  description = "Protect the database from deletion (true for anything beyond a demo)."
  type        = bool
  default     = false
}

variable "ingest_concurrency" {
  description = "Reserved concurrency of the ingest Lambda; bounds the connections it opens to RDS."
  type        = number
  default     = 5
}

variable "alarm_email" {
  description = "Email subscribed to alarm notifications. Empty = no subscription."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 30
}
