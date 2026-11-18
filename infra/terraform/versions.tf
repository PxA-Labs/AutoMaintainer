terraform {
  # 1.9+: variable validations may refer to other variables.
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }

  # State is local by default. For the shared production state, copy
  # backend.tf.example to backend.tf (a partial "gcs" backend) and run
  # `terraform init -backend-config="bucket=<state bucket>"`; see README.md.
}
