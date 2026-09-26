terraform {
  required_version = ">= 1.6.0"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.70.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }

  # Explicit, shared state path: without this, Terraform defaults to a
  # state file relative to whatever directory `tofu` runs in, which
  # differs between the runner's manual checkout and Atlantis's own
  # ephemeral per-PR clone -- they'd otherwise never see the same state.
  # /tfstate is mounted from the same host directory in both run.sh and
  # Atlantis's docker-compose.yml, so both read/write the same file.
  # Filename predates this directory's move to provisioning/baseline_image/;
  # left as-is to avoid a state migration on the live container.
  backend "local" {
    path = "/tfstate/vault-provision.tfstate"
  }
}

variable "pve_endpoint" {
  type = string
}

variable "pve_api_token" {
  type      = string
  sensitive = true
}

variable "pve_node_name" {
  type = string
}

variable "ssh_public_key" {
  type = string
}

variable "baseline_image_admin_ssh_key" {
  type = string
}

variable "baseline_image_vmid" {
  type    = number
  default = 133
}

variable "baseline_image_mac_address" {
  type = string
}

variable "baseline_image_hostname" {
  type    = string
  default = "baseline_image"
}

variable "baseline_image_host_type" {
  type    = string
  default = "Servers"
}

variable "pihole_service_url" {
  type = string
}

variable "pihole_api_key" {
  type      = string
  sensitive = true
}

variable "pve_storage_pool" {
  type    = string
  default = "local-zfs"
}

variable "pve_template_id" {
  type = string
}

provider "proxmox" {
  endpoint  = var.pve_endpoint
  api_token = var.pve_api_token
  insecure  = true
}

# 1. Provision target LXC container
resource "proxmox_virtual_environment_container" "baseline_image" {
  node_name     = var.pve_node_name
  vm_id         = var.baseline_image_vmid
  unprivileged  = true
  protection    = true
  start_on_boot = true
  tags          = ["baseline_image", "terraform-managed"]

  initialization {
    hostname = var.baseline_image_hostname

    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }

    user_account {
      keys = [var.ssh_public_key]
    }
  }

  network_interface {
    name        = "eth0"
    bridge      = "vmbr0"
    mac_address = var.baseline_image_mac_address
  }

  cpu {
    cores = 2
  }

  memory {
    dedicated = 512
    swap      = 0
  }

  disk {
    datastore_id = var.pve_storage_pool
    size         = 4
  }

  operating_system {
    template_file_id = var.pve_template_id
    type             = "debian"
  }

  depends_on = [null_resource.pihole_service_sync]
}

# 2. Register MAC and Hostname via Pi-hole Microservice
resource "null_resource" "pihole_service_sync" {
  triggers = {
    mac_address = var.baseline_image_mac_address
    hostname    = var.baseline_image_hostname
  }

  provisioner "local-exec" {
    command = "curl -fsSL -X POST \"${var.pihole_service_url}\" -H \"Authorization: Bearer ${var.pihole_api_key}\" -H \"Content-Type: application/json\" -d '{\"mac\": \"${var.baseline_image_mac_address}\", \"hostname\": \"${var.baseline_image_hostname}\", \"host_type\": \"${var.baseline_image_host_type}\"}'"
  }
}

# 3. Dynamic Ansible Inventory Output
resource "local_file" "ansible_inventory" {
  content = templatefile("${path.module}/inventory.ini.tpl", {
    baseline_image_hostname      = var.baseline_image_hostname
    baseline_image_admin_ssh_key = var.baseline_image_admin_ssh_key
  })
  filename = "${path.module}/inventory.ini"

  depends_on = [null_resource.pihole_service_sync]
}

output "assigned_mac" {
  value = var.baseline_image_mac_address
}

output "assigned_host" {
  value = var.baseline_image_hostname
}

# Preserves state continuity for the already-applied container after the
# vault -> baseline_image resource-label rename; without this, the next
# plan treats it as a delete-and-recreate instead of a no-op.
moved {
  from = proxmox_virtual_environment_container.vault
  to   = proxmox_virtual_environment_container.baseline_image
}
