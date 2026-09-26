# Deploying a New Service: End-to-End Walkthrough

This document covers how to get from an empty `services/` directory to a real, running, GitOps-managed service. It uses **Vault** as a real-world example to illustrate these concepts.

**Be sure to read these three guides first, in this order, before this one**:
1. [opentofu_ansible_setup_guide.md](opentofu_ansible_setup_guide.md) - How to set up the Runner instance that hosts OpenTofu and Ansible
2. [lxc_instance_provisioning_guide.md](lxc_instance_provisioning_guide.md) - Overview of LXC instance provisioning. Also covers the per-service file pattern: what each of the six tracked files is for, the rename recipe for a new service, the dependency-ordering fix, and more
3. [gitops_setup_guide.md](gitops_setup_guide.md) - Setting up Atlantis as the GitOps engine: architecture, running it, delivering webhooks, and the `atlantis.yaml`/`repos.yaml` config that ties a project to a workflow

## I. End-to-End Deployment Primer

A few grounding questions:

* **What does "end-to-end deployment" mean?**
  Starting from a service directory that exists only as files in this repo, and ending with a real, running, fully-configured instance of that service, having touched nothing by hand except opening a pull request and commenting on it.

* **What actually changes once GitOps is in place, day to day?**
  Before: someone SSHes into the runner and runs `tofu apply` / `ansible-playbook` by hand, from a checkout of the repo that has to be kept manually up to date (`gitops_setup_guide.md`'s earlier sections describe exactly this manual path, and it's still there as a fallback).
  After: a pull request *is* the trigger. Opening one gets you a plan comment; commenting `atlantis apply` runs the same commands automatically, in Atlantis's own container, against the real repo state at that PR's commit. `gitops_setup_guide.md` §6 covers this in full.

* **What is each of the six tracked files in a service directory actually for?**
  One line each, as a quick reference; `lxc_instance_provisioning_guide.md`'s own table has the full detail on which parts of each are generic versus service-specific.

| File | One-line purpose |
|---|---|
| `main.tf` | Defines the LXC container, its Pi-hole/DNS registration, and the rendered Ansible inventory. |
| `inventory.ini.tpl` | Template `main.tf` fills in with the real hostname/SSH key, producing the file Ansible actually reads. |
| `ansible.cfg` | Ansible's own settings (inventory location, host key checking). Identical for every service. |
| `deploy_<service>.yml` | The playbook that turns a bare container into a configured, running service. Entirely service-specific past three shared setup tasks. |
| `run.sh` | Wrapper for running `tofu`/`ansible-playbook` by hand from this directory, outside of Atlantis. |
| `terraform.tfvars.example` | Placeholder values for the real, gitignored `terraform.tfvars` this service needs. |

Three more points are worth clarifying:

* **Does deploying a service like Vault need a separate Terraform module or Ansible playbook?**
  * No separate *module*: `main.tf` is a complete, standalone file per service, not a caller of shared module code (see `lxc_instance_provisioning_guide.md` for why a module isn't worth the indirection at this repo's current scale).
  * A separate *playbook*, always: `deploy_vault.yml` is Vault-specific by design and could never be shared with another service, since it installs different software.

* **Where does Vault-specific content actually live?**
  Inside `provisioning/vault/`, distinguished by *content* rather than location:
  * Generic (the same shape every service's `main.tf` follows): the container resource, the Pi-hole registration, the rendered Ansible inventory, every variable prefixed `vault_`, `protection`/`start_on_boot`/`tags`, the dependency ordering that fixes the DHCP timing issue.
  * Vault-specific: the entire contents of `deploy_vault.yml` past its three shared bootstrap tasks (installing the `vault` package, configuring raft storage, running `vault operator init`/`unseal`, enabling the KV v2 engine).

	There's no separate "Vault config" file or directory elsewhere; it's all in this one place, which is exactly what makes the directory self-contained and independently deployable.

* **How is this automated end-to-end via GitOps, and what has to be set up per service?**
  Atlantis's `autodiscover` scans its own clone of this repository, not the runner's separate manual checkout, and only reacts to directories a given PR's diff actually touches: a directory becomes a discovered project once a file inside it changes, not from a standing scan of the whole tree. It does **not**, however, bind a project to the custom workflow that runs the Ansible step (`gitops_setup_guide.md` §5 covers the real failure this caused and its fix): that needs one explicit entry per service under `atlantis.yaml`'s `projects:` key, naming the same shared workflow every service uses:

  ```yaml
  projects:
  - dir: provisioning/vault
    workflow: lxc-instance
  ```

  That one entry is the extent of the per-service Atlantis config. Everything else, including the workflow definition itself, is written once and shared:

  ```yaml
  apply:
    steps:
      - apply
      - run: ansible-playbook deploy_*.yml
  ```

  `deploy_*.yml` is a glob, not a hardcoded filename, which is what makes that shared workflow able to serve every service without editing it per service: whatever single `deploy_<service>.yml` file exists in a project's directory is what runs. Add a new service, and this line finds its playbook automatically; the only thing you add to `atlantis.yaml` is that one `projects:` entry above.

## II. The full path, step by step

This is what actually happens, in order, when a change to Vault's config goes from a laptop to a running, configured instance. Each step names which document covers the mechanics in depth.

1. **Runner and Atlantis already running.** One-time infrastructure, covered by [opentofu_ansible_setup_guide.md](opentofu_ansible_setup_guide.md) and [gitops_setup_guide.md](gitops_setup_guide.md). Not repeated per service or per change.

2. **`provisioning/vault/` exists in the repo**, containing the six tracked files from Section 1's table, plus `.terraform.lock.hcl`. All committed. `terraform.tfvars` itself is not (gitignored; see step 6).

3. **Someone edits a tracked file** (`main.tf`, `deploy_vault.yml`, whatever), on a branch, and opens a PR against `main`.

4. **Atlantis receives the event** via `gh webhook forward` (no public endpoint involved, per [gitops_setup_guide.md](gitops_setup_guide.md) §4), matches `provisioning/vault` against its explicit `projects:` entry (Section 1), and runs the `lxc-instance` workflow's `plan` steps: `tofu init` then `tofu plan`, inside a fresh, temporary clone of the repo that Atlantis manages itself, separate from the runner's own long-lived checkout at `/opt/infra/infrastructure`.

5. **Atlantis comments the plan output on the PR.** This is the actual review gate: whoever's watching reads the diff here, not in a terminal.

6. **A `pre_workflow_hook` supplies `terraform.tfvars`** into that ephemeral clone before `plan` even runs (`gitops_setup_guide.md` §7), copying it from a fixed, persistent path on the Atlantis host. Without this, `tofu plan` would fail on every missing variable; with it, plan and apply both proceed normally. This was confirmed as a real failure during initial testing, and is now resolved.

7. **`atlantis apply`** (a PR comment, since `automerge: false`) runs `tofu apply`, creating or updating the container: Proxmox LXC provisioned, Pi-hole/DNS reservation registered *before* the container's first boot (the ordering fix that prevents the wrong-IP-until-reboot bug), `protection`/`start_on_boot`/`tags` set.

8. **The same `apply` workflow immediately runs `ansible-playbook deploy_vault.yml`**, in the same Atlantis container, no Docker socket involved (the custom Atlantis image has `ansible-core` baked in directly, per `gitops_setup_guide.md` §2). This is the step that turns a bare, freshly-booted Debian LXC into an actual, configured Vault: installs the HashiCorp apt repo and the `vault` package, writes `/etc/vault.d/vault.hcl` (raft storage, TCP listener, `disable_mlock`), starts the `vault` systemd service, runs `vault operator init` on first run only (`vault_health.status == 501`), unseals with the first three of five Shamir keys, enables the KV v2 secrets engine at `secret/`, and finishes with a health check that asserts Vault ends up unsealed and reachable, failing the play otherwise.

9. **Vault is now a real, running, initialized, unsealed service.** Root token and unseal keys land in `/secrets/vault-cluster-keys.json`, a fixed path shared across every PR's ephemeral clone, since a later PR's own clone needs to read this same file back on every subsequent apply (`gitops_setup_guide.md` §2 and §7 cover why).

## III. Verifying it worked

These are the exact checks used to confirm this pipeline actually worked:

```bash
# From the runner, or via SSH to the deployed instance:
source /etc/profile.d/vault.sh   # set by deploy_vault.yml
vault status                      # Initialized: true, Sealed: false
VAULT_TOKEN=<root-token> vault secrets list   # secret/ present, type kv
```

`docker logs atlantis` (on the runner) shows the actual `tofu init`/`plan`/`apply` output for any run; a still-running `ansible-playbook` doesn't stream to that log the same way, so `docker exec atlantis pgrep -f ansible-playbook` (still running) and the PR's own apply comment (final result, once it completes) are the more reliable checks for that specific step.

## IV. Doing this for a service that isn't Vault

The mechanical recipe, copying the generic template rather than Vault's own directory:

1. In a local clone of this repo, `git pull` to get the latest `main`, then copy `provisioning/baseline_image/` to `provisioning/<service>/`.
2. Rename every `baseline_image_*` variable, and the `baseline_image` resource label, to `<service>_*` / `<service>` (`main.tf`, `inventory.ini.tpl`, `terraform.tfvars.example`, `run.sh`'s mount path). [lxc_instance_provisioning_guide.md](lxc_instance_provisioning_guide.md) has the exact list of what needs renaming and what doesn't (`ansible.cfg` never changes).
3. Replace `deploy_baseline_image.yml`'s content past the three bootstrap tasks (`wait_for_connection`, gather facts, authorize admin key) with whatever actually installs and configures the new service. Rename the file to `deploy_<service>.yml`; the `deploy_*.yml` glob in the shared workflow picks it up automatically.
4. **Add one entry to `atlantis.yaml`'s `projects:` list**, naming the new directory and the same shared `workflow: lxc-instance` (Section 1). This is the one piece of Atlantis config every new service actually needs; nothing else there changes.
5. If this new service writes anything analogous to `vault-cluster-keys.json` (something one PR's run produces that a later PR's run needs to read back), give it its own fixed path under `/secrets`, following the same reasoning as step 9 in Section 2.
6. Follow Section 2 above from step 3 onward: open a PR, review the plan, apply, verify.
