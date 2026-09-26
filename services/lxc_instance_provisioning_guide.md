# LXC Instance Provisioning Guide

This guide covers the OpenTofu and Ansible pattern used to provision an LXC container on Proxmox VE, register it with the Pi-hole DHCP/DNS reservation microservice, and configure it with Ansible, from an empty service directory to a running, configured host. [services/vault-provision/](vault-provision/) is the worked example; to provision a different service, copy that directory's structure and substitute your own values and playbook.

This guide assumes the runner environment from [opentofu_ansible_setup_guide.md](opentofu_ansible_setup_guide.md) is already set up and this repository is cloned to `/opt/infra/infrastructure` on the runner.

## 1. Naming convention

Each instance gets its own directory at `services/<service>-provision/` (e.g. `services/vault-provision/`) and its own set of OpenTofu variables prefixed with the service name (e.g. `vault_vmid`, `vault_hostname`). Variables shared across every instance (Proxmox connection details, the orchestration SSH key, storage pool, template, and Pi-hole service credentials) keep their generic names (`pve_endpoint`, `ssh_public_key`, `pve_storage_pool`, `pve_template_id`, `pihole_service_url`, `pihole_api_key`) and can be copied between workspaces unchanged.

| Purpose | Variable pattern | Vault example |
|---|---|---|
| Container ID | `<service>_vmid` | `vault_vmid = 133` |
| Hostname | `<service>_hostname` | `vault_hostname = "vault"` |
| MAC address | `<service>_mac_address` | `vault_mac_address = "BC:24:11:0B:E1:31"` |
| Pi-hole reservation section | `<service>_host_type` | `vault_host_type = "Servers"` |
| Admin SSH key | `<service>_admin_ssh_key` | `vault_admin_ssh_key = "ssh-ed25519 ..."` |

Each service directory holds the same six files:

| File | Committed? | Edit per service? | Purpose |
|---|---|---|---|
| `main.tf` | Yes | Yes — rename the resource label and every `vault_*` variable to `<service>`/`<service>_*` | Resource definitions — see [vault-provision/main.tf](vault-provision/main.tf) |
| `inventory.ini.tpl` | Yes | Yes — group name and `templatefile()` variable names, matching `main.tf` | Ansible inventory template |
| `ansible.cfg` | Yes | No — identical across every instance | Ansible defaults |
| `deploy_<service>.yml` | Yes | Yes — written from scratch past the three bootstrap tasks in Section 5 | Service-specific playbook |
| `run.sh` | Yes | Yes — both `-v` host-path mounts must point at `services/<service>-provision` | Docker wrapper, pre-pointed at this workspace |
| `terraform.tfvars.example` | Yes | Yes — placeholder values and `<service>_*` variable names | Placeholder values — copy to `terraform.tfvars` |
| `terraform.tfvars` | **No** (gitignored) | — created once, by hand, on the runner | Real credentials |
| `terraform.tfstate` | **No** (gitignored) | — | Local state |

`ansible.cfg` is the only tracked file with no service-specific content — copy it unchanged. Every other tracked file has `vault`/`vault_*` references that must be replaced with the new service's name before use.

Rename the copied directory, then replace every `vault` reference across the tracked files in one pass — the `vault_` prefix (variable and template names) first, then the bare `vault` label (resource label, inventory group, and directory names):

```bash
cd services
cp -r vault-provision myservice-provision
cd myservice-provision
mv deploy_vault.yml deploy_myservice.yml
grep -rl 'vault' -- main.tf inventory.ini.tpl ansible.cfg deploy_myservice.yml run.sh terraform.tfvars.example \
  | xargs sed -i 's/vault_/myservice_/g; s/\bvault\b/myservice/g'
```

`sed -i` above is the GNU syntax (matches the Debian 12 runner from the setup guide); on macOS, use `sed -i ''` instead. Confirm nothing was missed:

```bash
grep -rn 'vault' main.tf inventory.ini.tpl ansible.cfg deploy_myservice.yml run.sh terraform.tfvars.example
```

No output means every reference was renamed.

## 2. main.tf: the resource-ordering rule

Read [services/vault-provision/main.tf](vault-provision/main.tf) for the full, current definition. Three points in it apply to every future instance, not just Vault:

**The Pi-hole reservation must be created before the container.** `null_resource.pihole_service_sync` has no `depends_on`, and `proxmox_virtual_environment_container.vault` has `depends_on = [null_resource.pihole_service_sync]`, the reverse of what you'd write if you provisioned the container first. This ordering matters: a container's first DHCP request happens as soon as it starts. If the DHCP/DNS reservation doesn't exist yet, that first lease can be a mismatched address that isn't corrected until the next lease renewal (e.g., on reboot). Creating the reservation first means the container's very first boot already resolves correctly.

**`protection = true`** blocks accidental destroy or forced replacement through OpenTofu, a safeguard for any instance holding state that can't be trivially recreated (Vault's raft storage, in this example). To make an intentional destructive change, set it to `false` first, apply, then make the change.

**`user_account.keys` holds only `var.ssh_public_key`** (the runner's orchestration key). Adding or changing keys in that list forces the whole container to be destroyed and recreated; OpenTofu can't update it in place. Any additional, human-facing key goes in via Ansible instead (Section 5), which updates `authorized_keys` in place with no destroy risk.

## 3. Generate a deterministic MAC address

Run on the Proxmox host to find a MAC that isn't already in use by another guest:

```bash
while MAC=$(printf 'BC:24:11:%02X:%02X:%02X\n' $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))
grep -rqsi "$MAC" /etc/pve/lxc /etc/pve/qemu-server; do :; done; echo "$MAC"
```

## 4. Set terraform.tfvars

On the runner, inside the service directory:

```bash
cd /opt/infra/infrastructure/services/vault-provision
cp terraform.tfvars.example terraform.tfvars
chmod 600 terraform.tfvars
```

Edit `terraform.tfvars` with real values: see [vault-provision/terraform.tfvars.example](vault-provision/terraform.tfvars.example) for the full set and what each one needs.

**`host_type` must match a section label already defined in the Pi-hole microservice's reservations file exactly** (case-insensitive). Check the valid labels before setting this:

```bash
ssh root@<pihole-host> "grep -E '^#.*\.[0-9]{1,3}\s+to\s+\.[0-9]{1,3}' /root/scripts/macaddr.txt"
```

Validate before applying:

```bash
./run.sh tofu fmt -check
./run.sh tofu init
./run.sh tofu validate
```

## 5. Ansible configuration

`ansible.cfg` and `deploy_<service>.yml` are tracked per-service. Every playbook in this pattern starts with the same three tasks, which the generated inventory and `main.tf` above already support:

```yaml
- name: Deploy and Initialize <Service>
  hosts: all
  gather_facts: false
  tasks:
    - name: Wait for container to acquire DHCP lease and open SSH
      ansible.builtin.wait_for_connection:
        delay: 5
        timeout: 180

    - name: Gather facts once SSH is online
      ansible.builtin.setup:

    - name: Authorize admin SSH key for direct human access
      ansible.builtin.lineinfile:
        path: /root/.ssh/authorized_keys
        line: "{{ admin_ssh_key }}"
        create: true
        mode: "0600"

    # ... service-specific tasks follow ...
```

`hosts: all` (not a named group) is deliberate: `inventory.ini.tpl` defines exactly one group per workspace, so `all` and that group are equivalent, and it means the group's name only has to exist in one file instead of being duplicated between the inventory template and every playbook.

The `admin_ssh_key` task uses `ansible.builtin.lineinfile`, part of `ansible-core`, rather than `ansible.posix.authorized_key`: the runner image only installs `ansible-core` (Section 3 of the setup guide), and `ansible.posix` is not a bundled collection.

For the complete worked example, installing HashiCorp Vault, configuring raft storage, initializing and unsealing the cluster, and enabling the KV v2 engine, see [vault-provision/deploy_vault.yml](vault-provision/deploy_vault.yml).

## 6. Run the pipeline

From the service directory on the runner:

```bash
# 1. Initialize
./run.sh tofu init

# 2. Review the plan
./run.sh tofu plan -detailed-exitcode

# 3. Provision the container and register its reservation
./run.sh tofu apply -auto-approve

# 4. Validate the rendered inventory and playbook syntax
./run.sh ansible-inventory --list
./run.sh ansible-playbook --syntax-check deploy_<service>.yml

# 5. Configure the service
./run.sh ansible-playbook deploy_<service>.yml
```

Check the running service at its assigned hostname once the playbook completes.

## 7. Making a change

Every file except `terraform.tfvars` and `terraform.tfstate` is version-controlled, so changes go through git rather than being edited directly on the runner:

1. Edit the relevant file(s) in a local clone of this repository.
2. Commit and push.
3. On the runner: `cd /opt/infra/infrastructure && git pull`.
4. Re-run `./run.sh tofu plan` from the service directory and review the diff before applying.

**Always read the plan before applying**, especially after changing anything under `initialization` or `network_interface` in `main.tf`: some attribute changes update the container in place, and others (like the `user_account.keys` case in Section 2) force it to be destroyed and recreated. `tofu plan` shows which before anything happens.

## 8. Troubleshooting

**`Error: error creating container: the requested resource does not exist`**
The `apply` referenced a node name, storage pool, template file ID, or bridge that doesn't exist on the target Proxmox node. Confirm each against the Proxmox API before retrying:

```bash
curl -sk -H "Authorization: PVEAPIToken=<user>@<realm>!<tokenid>=<uuid>" \
  https://<pve-host>:8006/api2/json/nodes/<node>/storage
```

**`Error: local-exec provisioner error ... curl: (22) ... 400`**
The Pi-hole reservation payload is missing or misnamed a required field. The microservice requires `mac`, `hostname`, and `host_type` (all non-empty strings): check the `local-exec` command's `-d` JSON body against those exact key names.

**`curl: (22) ... 404` with a `valid_host_types` list in the body**
`host_type` doesn't match any section label in the reservations file. Use the section-listing command in Section 4 to get the exact labels.

**Container comes up with the wrong IP, only fixed by a reboot**
Its first DHCP lease was requested before the Pi-hole reservation existed. This is prevented by the dependency order in Section 2 (`pihole_service_sync` before the container resource): if this happens on an already-running container, it keeps whatever lease it has until its next renewal or reboot; the ordering only protects future `destroy`/`apply` cycles.

**`tofu plan` shows `must be replaced` on the container after a config change**
Some attributes (notably `user_account.keys`) can't be updated in place and force a destroy/recreate. Read the plan output before applying; if you didn't intend a replacement, revert the change and find an in-place alternative (Section 2 shows the pattern for SSH keys specifically).

## 9. Known limitations

- **No reservation cleanup on destroy.** The Pi-hole microservice has no delete endpoint, so `tofu destroy` removes the container but leaves its MAC/hostname reservation in place. Remove the corresponding line from the reservations file manually if the address needs to be freed.
- **Package versions are unpinned.** Playbooks that `apt install` a service package (e.g. `vault`) get whatever version is current in the upstream repository at apply time. Pin a version if reproducibility across runs matters.
- **TLS is disabled** on service listeners configured by this pattern's example playbook (`tls_disable = 1` for Vault). Acceptable for a trusted internal network; terminate TLS in front of the service, or configure it directly, before exposing it more broadly.
