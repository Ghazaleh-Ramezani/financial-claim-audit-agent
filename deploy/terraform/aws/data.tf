# ---------------------------------------------------------------- container registries

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "MUTABLE"
  force_delete         = !var.db_deletion_protection

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "ingest" {
  name                 = "${local.name}-ingest"
  image_tag_mutability = "MUTABLE"
  force_delete         = !var.db_deletion_protection

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = { api = aws_ecr_repository.api.name, ingest = aws_ecr_repository.ingest.name }
  repository = each.value

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------- claim store (RDS PostgreSQL 16)

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "pg16" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
}

resource "aws_db_instance" "claims" {
  identifier     = "${local.name}-claims"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  db_name  = "claims"
  username = "claims"
  # The master password is generated and rotated by RDS in Secrets Manager; it never
  # appears in Terraform state, variables or task definitions.
  manage_master_user_password = true

  allocated_storage     = 20
  max_allocated_storage = 50
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.pg16.name
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period    = 7
  copy_tags_to_snapshot      = true
  auto_minor_version_upgrade = true
  deletion_protection        = var.db_deletion_protection
  skip_final_snapshot        = !var.db_deletion_protection
  final_snapshot_identifier  = var.db_deletion_protection ? "${local.name}-claims-final" : null

  enabled_cloudwatch_logs_exports = ["postgresql"]
}

locals {
  db_secret_arn = aws_db_instance.claims.master_user_secret[0].secret_arn
}

# ---------------------------------------------------------------- filings bucket

resource "aws_s3_bucket" "filings" {
  bucket        = "${local.name}-filings-${local.account_id}-${var.region}"
  force_destroy = !var.db_deletion_protection
}

resource "aws_s3_bucket_versioning" "filings" {
  bucket = aws_s3_bucket.filings.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "filings" {
  bucket = aws_s3_bucket.filings.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "filings" {
  bucket = aws_s3_bucket.filings.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "filings" {
  bucket = aws_s3_bucket.filings.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_policy" "filings_tls_only" {
  bucket = aws_s3_bucket.filings.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.filings.arn, "${aws_s3_bucket.filings.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.filings]
}

resource "aws_s3_bucket_lifecycle_configuration" "filings" {
  bucket = aws_s3_bucket.filings.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 365
    }
  }
}

# Every object event goes to EventBridge; the rule in ingest.tf selects incoming/ uploads.
resource "aws_s3_bucket_notification" "filings" {
  bucket      = aws_s3_bucket.filings.id
  eventbridge = true
}
