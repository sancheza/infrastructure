# Infrastructure Repository Architecture & Best Practices Review

**Date:** September 2026  
**Repository:** `infrastructure` (`/Users/asanchez/dev/infrastructure`)  
**Status:** In Progress — Initial quick fixes applied; comprehensive roadmap documented below for future review.

---

## 1. Quick Fixes Applied in Current Session

The following immediate findings were remediated directly:
1. **Metadata Alignment in `network_scanner.py`:**
   - Corrected the header module docstring from `network_inventory.py` to `network_scanner.py`.
   - Updated the CLI epilog usage example from `sudo ./network_inventory.py ...` to `sudo ./network_scanner.py ...`.
   - Added unit test suite `test_network_scanner.py` covering text truncation, port color classification, numeric IP sorting, CSV exporting, and metadata integrity.
2. **Elevated Privilege Check in `check_wifi.sh`:**
   - Implemented `show_help()` and CLI flag parsing (`-h`, `--help`) *prior* to platform and `$EUID` checks.
   - Users on macOS and Linux can now inspect usage and flag documentation without requiring `sudo`.

---

## 2. Deep Dive: Content & Organization Best Practices

### A. Infrastructure as Code (IaC) Template Desynchronization
The repository contains two service provisioning workspaces under `services/`:
* `services/vault-provision/` (actively maintained, production-tested)
* `services/baseline_image-provision/` (intended as the generic worked example/template)

Over recent commits, critical architectural fixes applied to `vault-provision` were not propagated to `baseline_image-provision`:

1. **Missing State Backend (`main.tf`):**
   * `vault-provision/main.tf` defines an explicit shared state backend:
     ```terraform
     backend "local" {
       path = "/tfstate/vault-provision.tfstate"
     }
     ```
   * `baseline_image-provision/main.tf` contains **no backend definition**, defaulting to local state inside the container/directory. In an Atlantis GitOps workflow where runs execute in throwaway clones, state is permanently lost between PR runs.
2. **Permission Failure Under Non-Root Runner (`inventory.ini.tpl`):**
   * `vault-provision/inventory.ini.tpl` was updated to reference `/keys/id_infra` because Atlantis runs as unprivileged user `atlantis` (UID 100), which cannot read `/root/.ssh/` (mode 0700).
   * `baseline_image-provision/inventory.ini.tpl` still hardcodes `/root/.ssh/id_infra`, which will fail with `Permission denied` under Atlantis.
3. **Outdated Runner Wrapper (`run.sh`):**
   * `vault-provision/run.sh` mounts `/tfstate`, `/secrets`, and `/root/.ssh:/keys:ro`.
   * `baseline_image-provision/run.sh` is missing the `/tfstate`, `/secrets`, and `/keys` volume mounts.
4. **Playbook Identity & Secrets Leak Risk (`deploy_baseline_image.yml`):**
   * `deploy_baseline_image.yml` is an outdated copy of `deploy_vault.yml` that literally installs HashiCorp Vault.
   * Furthermore, it writes unseal keys to `{{ playbook_dir }}/vault-cluster-keys.json` instead of `/secrets/vault-cluster-keys.json`, which breaks ephemeral Atlantis environments and risks committing keys into git if `.gitignore` rules fail.
5. **Resource Collisions (`terraform.tfvars.example`):**
   * `baseline_image-provision/terraform.tfvars.example` still specifies `baseline_image_vmid = 133` and `baseline_image_mac_address = "BC:24:11:0B:E1:31"`, which collide with the running Vault instance.

### B. CI/CD & Testing Infrastructure
The GitHub Actions workflow in `.github/workflows/lint.yml` exhibits critical coverage gaps:
1. **Tests Never Execute in CI:**
   * The workflow installs `requirements.txt` (which includes `pytest`), but only invokes `flake8` and `shellcheck`.
   * The existing 53 test cases (`test_macaddr_reservation_service.py`, `test_pihole_importer.py`, `test_network_scanner.py`) never run during PR validation or on pushes to `main`.
2. **No IaC or Playbook Linting:**
   * OpenTofu syntax and formatting (`tofu fmt -check`, `tofu validate`) are not checked.
   * Ansible playbooks are not linted (`ansible-lint`).
3. **No Documentation Build Verification:**
   * Sphinx documentation is not compiled in CI (`sphinx-build -W docs docs/_build`), meaning broken cross-references or RST syntax errors will pass unchecked.
4. **Standards Divergence:**
   * `CONTRIBUTING.md` specifies `pylint`, whereas CI executes `flake8`.

### C. CLI Interface Consistency & Coding Standards
1. **Verbosity Flag (`-v`) Collisions:**
   * Across `network_scanner.py`, `pihole_importer.py`, and `macaddr_reservation_service.py`, `-v` is bound to `--version`.
   * Per CLI guidelines and standard conventions, `-v` should control verbosity/debug logging, while version information should be reserved for `-V` / `--version`.
2. **Missing CLI Capabilities:**
   * `check_latency.py` lacks a `-v` / `--verbose` option.
   * `monitor_smb.sh` and `tvh_kuma_monitor.sh` have no argument handling or help options.
3. **Privilege Awareness:**
   * `check_latency.py` invokes system ping with fractional intervals (`-i 0.2`). On macOS and Linux, sending packets at intervals `< 1.0s` or `< 0.2s` requires superuser (`sudo`) privileges, but the script does not warn or check for root before running.

---

## 3. File Placement & Architecture Layout

### Current State Issues
1. **Root Directory Congestion:**
   Nine distinct scripts across three programming languages (Bash, Python, PowerShell), systemd service unit files, test modules, and repository configuration files all reside in the repository root.
2. **Test Co-location:**
   `test_*.py` files are intermingled with operational production scripts in the root directory rather than isolated in `tests/`.
3. **Systemd Services in Root:**
   `macaddr-reservation-service.service` and `tvh-monitor.service` belong in a dedicated `systemd/` or `deploy/` directory.
4. **Documentation Bifurcation:**
   * Operational scripts are documented in Sphinx (`docs/`).
   * Infrastructure provisioning guides (`deploying_a_new_service.md`, `lxc_instance_provisioning_guide.md`, `gitops_setup_guide.md`, `opentofu_ansible_setup_guide.md`) are located in `services/`.
   * `docs/index.rst` does not index the guides or `check_latency_win.ps1`.
5. **Orphaned Directories:**
   `assets/` in root is completely empty and untracked.

### Recommended Target Repository Structure
```
infrastructure/
├── .github/
│   └── workflows/
│       ├── lint.yml
│       └── test.yml
├── docs/                        # Unified Sphinx documentation
│   ├── conf.py
│   ├── index.rst
│   ├── guides/                  # Relocated Markdown/RST provisioning guides
│   │   ├── deploying_a_new_service.md
│   │   ├── gitops_setup_guide.md
│   │   ├── lxc_instance_provisioning_guide.md
│   │   └── opentofu_ansible_setup_guide.md
│   └── tools/                   # Per-tool documentation pages
├── scripts/                     # Grouped operational utilities
│   ├── network/
│   │   ├── network_scanner.py
│   │   ├── check_wifi.sh
│   │   └── monitor_smb.sh
│   ├── latency/
│   │   ├── check_latency.py
│   │   └── check_latency_win.ps1
│   ├── pihole/
│   │   ├── pihole_importer.py
│   │   └── macaddr_reservation_service.py
│   └── monitoring/
│       ├── tvh_kuma_monitor.sh
│       └── proxmox_net_check.sh
├── services/                    # IaC and deployment pipelines
│   ├── runner-image/            # Runner Dockerfile & Atlantis configuration
│   ├── baseline_image-provision/
│   └── vault-provision/
├── systemd/                     # Systemd service unit files
│   ├── macaddr-reservation-service.service
│   └── tvh-monitor.service
├── tests/                       # Centralized pytest suite & fixtures
│   ├── test_macaddr_reservation_service.py
│   ├── test_network_scanner.py
│   └── test_pihole_importer.py
├── atlantis.yaml
├── CONTRIBUTING.md
├── LICENSE
├── README.md
└── requirements.txt
```

---

## 4. What Is Glaringly Missing

1. **Automated Test Execution in CI:**
   * The CI pipeline must run `pytest` automatically on PRs and commits to `main`.
2. **Test Coverage for Core Scripts:**
   * `check_latency.py` has no unit tests for ping output parsing, regex extraction, or statistical aggregation.
   * Shell scripts (`proxmox_net_check.sh`, `check_wifi.sh`, `monitor_smb.sh`, `tvh_kuma_monitor.sh`) have no automated test coverage.
3. **`atlantis.yaml` Registration for `baseline_image-provision`:**
   * Currently, only `services/vault-provision` is registered with workflow `lxc-instance`. PRs affecting `baseline_image-provision` fallback to default plan/apply without executing the Ansible configuration playbook.
4. **Dynamic Secret Mapping in Atlantis:**
   * `services/runner-image/repos.yaml` hardcodes a pre-workflow hook specifically for Vault:
     `cp /atlantis/secrets/vault-provision.tfvars $DIR/services/vault-provision/terraform.tfvars`.
   * A generalized convention (e.g. `cp /atlantis/secrets/${PROJECT_NAME}.tfvars $DIR/terraform.tfvars`) is needed for multi-service scaling.
5. **Tooling Configuration File:**
   * A `pyproject.toml` file defining tool configurations (`pytest`, `ruff`/`flake8`) in a single declarative location.

---

## 5. Prioritized Implementation Roadmap

### Priority 1: High (Correctness, CI/CD & IaC Alignment)
1. **Enable Pytest in CI:**
   Add `pytest` execution step to `.github/workflows/lint.yml`.
2. **Bring `services/baseline_image-provision` to Parity:**
   * Add `backend "local"` block pointing to `/tfstate/baseline_image-provision.tfstate`.
   * Update `inventory.ini.tpl` to use `/keys/id_infra`.
   * Update `run.sh` to mount `/tfstate`, `/secrets`, and `/keys`.
   * Replace Vault-specific steps in `deploy_baseline_image.yml` with generic baseline OS hardening tasks (or document clear placeholder tasks).
   * Register `services/baseline_image-provision` in `atlantis.yaml`.

### Priority 2: Medium (Layout Reorganization & Testing)
3. **Directory Restructuring:**
   * Create `tests/` and move `test_*.py` files into it.
   * Create `systemd/` and move `.service` files into it.
   * Create `scripts/` (or `tools/`) and organize utilities by operational domain.
4. **Implement Tests for `check_latency.py`:**
   * Add `test_check_latency.py` to test `parse_ping_output()` across macOS and Linux sample ping outputs.
5. **CLI Flag Normalization:**
   * Switch version flag to `-V` / `--version` across Python scripts and reserve `-v` / `--verbose` for verbose logging.

### Priority 3: Low (Hygiene & Documentation Consolidation)
6. **Integrate Guides into Sphinx:**
   * Cross-link Markdown guides from `services/` into `docs/index.rst`.
   * Add `check_latency_win.ps1` reference documentation to Sphinx.
7. **Clean Up Unused Artifacts:**
   * Remove empty `assets/` directory or add a documented `.gitkeep`.
   * Update `network_scanner.py` CSV export to allow specifying an output directory instead of defaulting to the repository root.
