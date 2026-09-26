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

variable "vault_admin_ssh_key" {
  type = string
}

variable "vault_vmid" {
  type    = number
  default = 133
}

variable "vault_mac_address" {
  type = string
}

variable "vault_hostname" {
  type    = string
  default = "vault"
}

variable "vault_host_type" {
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

# 1. Provision target Vault LXC container
resource "proxmox_virtual_environment_container" "vault" {
  node_name     = var.pve_node_name
  vm_id         = var.vault_vmid
  unprivileged  = true
  protection    = true
  start_on_boot = true
  tags          = ["vault", "terraform-managed"]

  initialization {
    hostname = var.vault_hostname

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
    mac_address = var.vault_mac_address
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
    mac_address = var.vault_mac_address
    hostname    = var.vault_hostname
  }

  provisioner "local-exec" {
    command = "curl -fsSL -X POST \"${var.pihole_service_url}\" -H \"Authorization: Bearer ${var.pihole_api_key}\" -H \"Content-Type: application/json\" -d '{\"mac\": \"${var.vault_mac_address}\", \"hostname\": \"${var.vault_hostname}\", \"host_type\": \"${var.vault_host_type}\"}'"
  }
}

# 3. Dynamic Ansible Inventory Output
resource "local_file" "ansible_inventory" {
  content = templatefile("${path.module}/inventory.ini.tpl", {
    vault_hostname      = var.vault_hostname
    vault_admin_ssh_key = var.vault_admin_ssh_key
  })
  filename = "${path.module}/inventory.ini"

  depends_on = [null_resource.pihole_service_sync]
}

output "assigned_mac" {
  value = var.vault_mac_address
}

output "assigned_host" {
  value = var.vault_hostname
}

# atlantis-test-1: trivial comment-only change to trigger a real
# Atlantis plan for end-to-end GitOps pipeline verification.
