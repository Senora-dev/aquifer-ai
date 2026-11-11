module "aquifer" {
  source = "./modules/aquifer"

  name                    = var.name
  repos                   = var.repos
  embedding_model_id      = var.embedding_model_id
  extraction_model_id     = var.extraction_model_id
  instance_type           = var.instance_type
  data_volume_gb          = var.data_volume_gb
  sync_interval_minutes   = var.sync_interval_minutes
  snapshot_retention_days = var.snapshot_retention_days
  log_retention_days      = var.log_retention_days
  domain                  = var.domain
  https_allowed_cidrs     = var.https_allowed_cidrs
  wheel_path              = "${path.module}/${var.wheel_path}"
  extra_env               = var.extra_env
}

# Deployments created before the module existed kept these resources at the root. `moved`
# re-addresses them in state: nothing is destroyed or recreated, and the data volume is kept.
moved {
  from = aws_vpc.this
  to   = module.aquifer.aws_vpc.this
}

moved {
  from = aws_internet_gateway.this
  to   = module.aquifer.aws_internet_gateway.this
}

moved {
  from = aws_subnet.public
  to   = module.aquifer.aws_subnet.public
}

moved {
  from = aws_route_table.public
  to   = module.aquifer.aws_route_table.public
}

moved {
  from = aws_route_table_association.public
  to   = module.aquifer.aws_route_table_association.public
}

moved {
  from = aws_security_group.server
  to   = module.aquifer.aws_security_group.server
}

moved {
  from = aws_iam_role.server
  to   = module.aquifer.aws_iam_role.server
}

moved {
  from = aws_iam_role_policy_attachment.ssm_core
  to   = module.aquifer.aws_iam_role_policy_attachment.ssm_core
}

moved {
  from = aws_iam_role_policy.server
  to   = module.aquifer.aws_iam_role_policy.server
}

moved {
  from = aws_iam_instance_profile.server
  to   = module.aquifer.aws_iam_instance_profile.server
}

moved {
  from = aws_s3_bucket.artifacts
  to   = module.aquifer.aws_s3_bucket.artifacts
}

moved {
  from = aws_s3_bucket_public_access_block.artifacts
  to   = module.aquifer.aws_s3_bucket_public_access_block.artifacts
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.artifacts
  to   = module.aquifer.aws_s3_bucket_server_side_encryption_configuration.artifacts
}

moved {
  from = aws_s3_object.wheel
  to   = module.aquifer.aws_s3_object.wheel
}

moved {
  from = aws_ssm_parameter.config
  to   = module.aquifer.aws_ssm_parameter.config
}

moved {
  from = aws_cloudwatch_log_group.server
  to   = module.aquifer.aws_cloudwatch_log_group.server
}

moved {
  from = aws_ebs_volume.data
  to   = module.aquifer.aws_ebs_volume.data
}

moved {
  from = aws_instance.server
  to   = module.aquifer.aws_instance.server
}

moved {
  from = aws_volume_attachment.data
  to   = module.aquifer.aws_volume_attachment.data
}

moved {
  from = aws_eip.server
  to   = module.aquifer.aws_eip.server
}

moved {
  from = aws_iam_role.dlm
  to   = module.aquifer.aws_iam_role.dlm
}

moved {
  from = aws_iam_role_policy_attachment.dlm
  to   = module.aquifer.aws_iam_role_policy_attachment.dlm
}

moved {
  from = aws_dlm_lifecycle_policy.data
  to   = module.aquifer.aws_dlm_lifecycle_policy.data
}
