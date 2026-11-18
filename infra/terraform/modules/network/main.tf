# Private network shared by the worker VM and Cloud Run's Direct VPC egress.
# The VM has no public IP; Cloud NAT gives it outbound access (GitHub, Groq,
# Supabase, Artifact Registry through Private Google Access).

variable "name" {
  description = "Name prefix."
  type        = string
}

variable "region" {
  description = "Region of the subnet, router and NAT."
  type        = string
}

variable "subnet_cidr" {
  description = "Primary range of the subnet."
  type        = string
}

resource "google_compute_network" "vpc" {
  name                    = "${var.name}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "main" {
  name                     = "${var.name}-${var.region}"
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_router" "router" {
  name    = "${var.name}-router"
  network = google_compute_network.vpc.id
  region  = var.region
}

resource "google_compute_router_nat" "nat" {
  name                               = "${var.name}-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.main.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

output "network_id" {
  value = google_compute_network.vpc.id
}

output "network_name" {
  value = google_compute_network.vpc.name
}

output "subnet_id" {
  value = google_compute_subnetwork.main.id
}

output "subnet_cidr" {
  value = google_compute_subnetwork.main.ip_cidr_range
}
