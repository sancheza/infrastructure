# OpenTofu & Ansible Runner Setup Guide

This guide sets up a dedicated LXC container ("the runner") that holds a Docker-based OpenTofu and Ansible toolchain for provisioning infrastructure on Proxmox VE. Once set up, the runner executes OpenTofu plans and Ansible playbooks inside a container, so no OpenTofu/Ansible dependencies are installed on the Proxmox host itself or on any workstation.

This is a one-time setup. After completing it, use `git pull` to update the runner's checkout of this repository, and each service's own `run.sh` wrapper (see [lxc_instance_provisioning_guide.md](lxc_instance_provisioning_guide.md)) for every OpenTofu or Ansible invocation.

## 1. Provision the runner LXC container

On the Proxmox VE web shell or host CLI, create a container with these settings:

| Setting | Value |
|---|---|
| CT ID | 100 |
| Hostname | `runner-01` |
| Template | `debian-12-standard` |
| Unprivileged | Yes |
| Nesting | Enabled (Options → Features → check Nesting and Keyctl; required for Docker inside LXC) |
| CPU cores | 2 |
| Memory | 2048 MB |
| Swap | 512 MB |
| Disk | 15 GB |
| Network | DHCP, or a static management IP |

Start the container and log into its shell:

```bash
pct start 100
pct enter 100
```

Install Docker Engine and git inside the runner LXC:

```bash
apt-get update && apt-get install -y ca-certificates curl gnupg git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io
```

`git` runs on the LXC host directly (not inside the Docker image built in Section 2) — it manages the checkout that the Docker container mounts as its workspace.

## 2. Build the runner Docker image

On the runner LXC, create the build directory and Dockerfile:

```bash
mkdir -p /opt/infra-runner
cd /opt/infra-runner
```

`/opt/infra-runner/Dockerfile`:

```dockerfile
FROM ghcr.io/opentofu/opentofu:minimal AS tofu-bin

FROM debian:12-slim

# Copy OpenTofu binary
COPY --from=tofu-bin /usr/local/bin/tofu /usr/local/bin/tofu

# Install Ansible, Python, SSH client, and tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    openssh-client \
    curl \
    jq \
    ca-certificates \
    bash \
    && rm -rf /var/lib/apt/lists/*

# Install Ansible core and hvac
RUN pip3 install --no-cache-dir --break-system-packages \
    ansible-core \
    hvac

WORKDIR /workspace
ENTRYPOINT ["/bin/bash"]
```

Build the image:

```bash
docker build -t infra-runner:latest /opt/infra-runner
```

`ansible-core` is the only Ansible package installed — no extra collections (e.g. `ansible.posix`). Playbooks in this pipeline stick to `ansible.builtin.*` modules for that reason.

## 3. Generate the orchestration SSH key

On the runner LXC, generate a dedicated SSH key pair. This key is used by OpenTofu (as `ssh_public_key`) and by Ansible to reach every host the runner provisions. It's a machine credential, not a personal one — it's never committed to git and never leaves this host.

```bash
mkdir -p /root/.ssh
ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_infra
```

Display the public key and copy its value for `terraform.tfvars` in each service workspace:

```bash
cat /root/.ssh/id_infra.pub
```

## 4. Clone the infrastructure repository

```bash
mkdir -p /opt/infra
git clone https://github.com/sancheza/infrastructure.git /opt/infra/infrastructure
```

Every provisioning workspace lives under `/opt/infra/infrastructure/services/<service>-provision/` (see [lxc_instance_provisioning_guide.md](lxc_instance_provisioning_guide.md)). Each workspace's `run.sh` wrapper is tracked in the repo and already points at its own workspace path, so no wrapper needs to be hand-written.

`terraform.tfvars` (real credentials) is gitignored and does not come from the clone. For each workspace, create it once on the runner from the tracked `.example` file:

```bash
cd /opt/infra/infrastructure/services/vault-provision
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars with real values
```

## 5. Keeping the runner in sync

Whenever `services/` files change upstream, update the runner's checkout before the next `apply`:

```bash
cd /opt/infra/infrastructure
git pull
```

`terraform.tfvars` and any `*.tfstate*` files are gitignored, so `git pull` never touches or overwrites them.
