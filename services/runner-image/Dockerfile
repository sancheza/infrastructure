FROM ghcr.io/opentofu/opentofu:minimal AS tofu-bin

FROM debian:12-slim

# Copy OpenTofu binary from official minimal image
COPY --from=tofu-bin /usr/local/bin/tofu /usr/local/bin/tofu

# Install Ansible, Python, SSH client, and utilities
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

# Install Ansible and hvac (Vault client library for Python)
RUN pip3 install --no-cache-dir --break-system-packages \
    ansible-core \
    hvac

WORKDIR /workspace
ENTRYPOINT ["/bin/bash"]
