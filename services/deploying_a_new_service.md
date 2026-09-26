# Deploying a New Service: End-to-End Walkthrough

This document answers the question the other guides in `services/` leave implicit: how do you actually get from an empty `services/` directory to a real, running, GitOps-managed service? It uses **Vault** as the worked example, since Vault is the one service this repo has actually deployed, applied, and verified end-to-end. Everything here has been tested against the real runner and a real GitHub PR, not just written and assumed.

Read this after [opentofu_ansible_setup_guide.md](opentofu_ansible_setup_guide.md) (runner setup) and [gitops_setup_guide.md](gitops_setup_guide.md) (Atlantis) are both done. [lxc_instance_provisioning_guide.md](lxc_instance_provisioning_guide.md) covers the *generic* LXC-provisioning pattern in isolation; this document is about what wraps around it to make a specific, complete, automated service.

## 1. The three questions this answers

**Does deploying a service like Vault need a separate Terraform module or Ansible playbook?**
No separate *module*: `main.tf` is a single, complete, standalone file per service, not a caller of shared module code (see [lxc_instance_provisioning_guide.md](lxc_instance_provisioning_guide.md) for why: at this repo's current scale, one instance, the added indirection of a module isn't worth it yet). A separate *playbook*, yes, always: `deploy_vault.yml` is Vault-specific by design and could never be shared with another service, since it installs different software.

**Where does Vault-specific content actually live?**
In the same directory as the generic LXC-provisioning bits, `services/vault-provision/`, distinguished by *content*, not by location:
- Generic (the same shape every service's `main.tf` follows): the container resource, the Pi-hole registration, the rendered Ansible inventory, `protection`/`start_on_boot`/`tags`, the dependency ordering that fixes the DHCP timing issue.
- Vault-specific: every variable prefixed `vault_` (`vault_vmid`, `vault_hostname`, `vault_mac_address`, `vault_host_type`, `vault_admin_ssh_key`), and the entire contents of `deploy_vault.yml` past its three shared bootstrap tasks (installing the `vault` package, configuring raft storage, running `vault operator init`/`unseal`, enabling the KV v2 engine).

There's no separate "Vault config" file or directory elsewhere; it's all in this one place, which is exactly what makes the directory self-contained and independently deployable.

**How is this automated end-to-end via GitOps?**
Atlantis (set up in [gitops_setup_guide.md](gitops_setup_guide.md)) needs zero Vault-specific configuration of its own. `atlantis.yaml`'s `autodiscover: mode: auto` finds any directory containing `.tf` files, including `services/vault-provision/`, without being told about it by name. The one shared `lxc-instance` workflow it applies is entirely generic:

```yaml
apply:
  steps:
    - apply
    - run: ansible-playbook deploy_*.yml
```

`deploy_*.yml` is a glob, not a hardcoded filename: whatever single `deploy_<service>.yml` exists in a project's directory is what runs. This is the actual mechanism that lets one Atlantis config serve every current and future service without being edited per service: the *orchestration* is generic and shared, the *content* it orchestrates is per-service.

## 2. The full path, step by step

This is what actually happens, in order, when a change to Vault's config goes from a laptop to a running, configured instance. Each step names which document covers the mechanics in depth.

1. **Runner and Atlantis already running.** One-time infrastructure, covered by [opentofu_ansible_setup_guide.md](opentofu_ansible_setup_guide.md) and [gitops_setup_guide.md](gitops_setup_guide.md). Not repeated per service or per change.

2. **`services/vault-provision/` exists in the repo**, containing `main.tf`, `inventory.ini.tpl`, `ansible.cfg`, `deploy_vault.yml`, `run.sh`, `terraform.tfvars.example`. All committed and tracked. `terraform.tfvars` itself is not (gitignored by design; see step 6).

3. **Someone edits a tracked file** (`main.tf`, `deploy_vault.yml`, whatever), on a branch, and opens a PR against `main`.

4. **Atlantis receives the event** via `gh webhook forward` (no public endpoint involved, per [gitops_setup_guide.md](gitops_setup_guide.md) §4), autodiscovers `services/vault-provision/` as a changed project, and runs the `lxc-instance` workflow's `plan` steps: `tofu init` then `tofu plan`, inside its own ephemeral clone of the repo (not the runner's long-lived `/opt/infra/infrastructure` checkout).

5. **Atlantis comments the plan output on the PR.** This is the actual review gate: whoever's watching reads the diff here, not in a terminal.

6. **`terraform.tfvars` doesn't exist in that ephemeral clone** (it's gitignored), so at this point `tofu plan` fails with `No value for required variable` for every variable it would supply. This is real, confirmed behavior, not a hypothetical: a test PR against this exact setup reached exactly this failure and nothing else. It's documented as a deliberately deferred gap in [gitops_setup_guide.md](gitops_setup_guide.md) §7, with a `pre_workflow_hook` stopgap given there. Until that's in place, this step is where the automated path currently stops for Vault specifically; steps 7 onward describe what happens once it's supplied (either via that hook, or by testing this today with a manual `atlantis plan`/`apply` from a workspace where `terraform.tfvars` already exists, as was done for the initial Vault deployment).

7. **Once variables are available, `atlantis apply`** (a PR comment, since `automerge: false`) runs `tofu apply`, creating or updating the container: Proxmox LXC provisioned, Pi-hole/DNS reservation registered *before* the container's first boot (the ordering fix that prevents the wrong-IP-until-reboot bug), `protection`/`start_on_boot`/`tags` set.

8. **The same `apply` workflow immediately runs `ansible-playbook deploy_vault.yml`**, in the same Atlantis container, no Docker socket involved (the custom Atlantis image has `ansible-core` baked in directly). This is the step that turns a bare, freshly-booted Debian LXC into an actual, configured Vault: installs the HashiCorp apt repo and the `vault` package, writes `/etc/vault.d/vault.hcl` (raft storage, TCP listener, `disable_mlock`), starts the `vault` systemd service, runs `vault operator init` on first run only (`vault_health.status == 501`), unseals with the first three of five Shamir keys, enables the KV v2 secrets engine at `secret/`, and finishes with a health check that fails the whole play if Vault doesn't end up unsealed and reachable rather than reporting success regardless.

9. **Vault is now a real, running, initialized, unsealed service.** Root token and unseal keys land in `vault-cluster-keys.json` on the Atlantis host (`/opt/infra/atlantis-data/...`, mode `0600`, gitignored). See [gitops_setup_guide.md](gitops_setup_guide.md)'s note on where to keep a durable copy of secrets like this one; the same reasoning applies here.

## 3. Verifying it worked

These are the exact checks used to confirm this pipeline for real, not a hypothetical list:

```bash
# From the runner, or via SSH to the deployed instance:
source /etc/profile.d/vault.sh   # set by deploy_vault.yml
vault status                      # Initialized: true, Sealed: false
VAULT_TOKEN=<root-token> vault secrets list   # secret/ present, type kv
```

`docker logs atlantis` (on the runner) shows the actual `tofu init`/`plan`/`apply` and `ansible-playbook` output for any run, including which project Atlantis identified and which workflow steps it ran.

## 4. Doing this for a service that isn't Vault

The mechanical recipe, using today's actual files as the reference (a dedicated, non-deployed template for this is separate, ongoing work elsewhere in this repo; use whatever exists once it lands, but the recipe itself doesn't change):

1. Copy `services/vault-provision/` to `services/<new-service>-provision/`.
2. Rename every `vault_*` variable, and the `vault` resource label, to `<new-service>_*` / `<new-service>` (`main.tf`, `inventory.ini.tpl`, `terraform.tfvars.example`, `run.sh`'s mount path). [lxc_instance_provisioning_guide.md](lxc_instance_provisioning_guide.md) has the exact list of what needs renaming and what doesn't (`ansible.cfg` never changes).
3. Replace `deploy_vault.yml`'s content past the three bootstrap tasks (`wait_for_connection`, gather facts, authorize admin key) with whatever actually installs and configures the new service. Rename the file to `deploy_<new-service>.yml`; the `deploy_*.yml` glob in `atlantis.yaml`'s workflow picks it up automatically, no Atlantis config change needed.
4. Nothing in `atlantis.yaml`, `repos.yaml`, or the Atlantis container itself needs to change. `autodiscover` finds the new directory; the shared workflow applies to it the same way.
5. Follow section 2 above from step 3 onward: open a PR, review the plan, apply, verify.
