# GitOps Setup Guide: Atlantis

This guide sets up Atlantis as the GitOps engine for this repo's OpenTofu/Ansible pipelines: a merged pull request drives `tofu plan`/`apply` and the matching `ansible-playbook` run, with plan output posted back to the PR. No manual `git pull` or `tofu apply` on the runner host once this is in place.

This assumes [opentofu_ansible_setup_guide.md](opentofu_ansible_setup_guide.md) is already done: Docker, the orchestration SSH key, and this repo cloned to `/opt/infra/infrastructure` on the runner via

```bash
git clone https://github.com/sancheza/infrastructure.git /opt/infra/infrastructure
```

**If that checkout already exists** (it will, on a runner you've used before), don't re-clone: update it instead.

```bash
cd /opt/infra/infrastructure && git pull
```

Do this before following any step below whose file was added or changed after your last pull; e.g. `services/runner-image/Dockerfile.atlantis` and `services/runner-image/docker-compose.yml` (§2-3) only exist once this pull has happened.

## 1. Architecture

```
GitHub (pull_request / issue_comment events on sancheza/infrastructure)
        │  normal webhook delivery, signed with a shared secret
        ▼
GitHub's own relay infrastructure
        │  outbound WebSocket, opened FROM the runner. GitHub never
        │  connects in: no port opened, no Funnel/tunnel involved.
        ▼
`gh webhook forward` (systemd service, on the runner)
        │  POSTs the forwarded event to Atlantis's own HTTP listener,
        │  which only ever binds 127.0.0.1, never reachable from
        │  outside this host
        ▼
Atlantis (Docker container, on the runner)
        │  plan/apply steps run `tofu` and `ansible-playbook` directly,
        │  as processes inside this one already-running container
        ▼
Proxmox API / target LXC hosts (unchanged from the manual pipeline)
```

Nothing about `main.tf`, `deploy_<service>.yml`, or the manual `./run.sh` workflow changes. Atlantis becomes a second, automated way to invoke the same commands you'd otherwise run by hand from a workspace directory.

## 2. Why a custom Atlantis image, not the stock one

Atlantis's official images (`ghcr.io/runatlantis/atlantis`) bundle Terraform (or, in some variants, OpenTofu) but never Ansible, and don't include the Docker CLI. Two ways to get `ansible-playbook` available to Atlantis's workflow steps:

- **Docker-outside-of-Docker**: mount the host's `/var/run/docker.sock` into Atlantis's container so its `run:` steps can `docker run infra-runner:latest ansible-playbook ...`, reusing the existing image from the setup guide.
- **A custom Atlantis image** with OpenTofu and `ansible-core` installed directly inside it.

**Use the custom image.** Mounting the Docker socket hands anything that can trigger an Atlantis workflow step effective root on the host: a PR comment shouldn't have that much reach. The cost is maintaining a second, similar Dockerfile; that's a small, contained tradeoff against a real privilege-escalation surface.

**This changes how Atlantis executes commands, compared to the manual `run.sh` path.** `run.sh` (§3 of [opentofu_ansible_setup_guide.md](opentofu_ansible_setup_guide.md)) spawns a fresh, disposable `infra-runner` container per command. Atlantis does not: its own container runs continuously (§3 below), and because `tofu`/`ansible-core` are installed directly inside its image, workflow steps (§5) invoke them as plain processes in that one already-running container, not by spawning anything new. No Docker socket, no nested containers, for the `tofu`/`ansible-playbook` steps themselves.

The Dockerfile lives at [services/runner-image/Dockerfile.atlantis](runner-image/Dockerfile.atlantis) in this repo, alongside [services/runner-image/Dockerfile](runner-image/Dockerfile) (the `infra-runner` image from the setup guide):

```dockerfile
FROM ghcr.io/runatlantis/atlantis:latest-debian-slim

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    jq \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages \
    ansible-core \
    hvac

# OpenTofu itself, matching the version already pinned in
# services/*/.terraform.lock.hcl
COPY --from=ghcr.io/opentofu/opentofu:minimal /usr/local/bin/tofu /usr/local/bin/tofu

USER atlantis
```

The `-debian-slim` tag ships no bundled Terraform/OpenTofu binary at all, deliberately: the `COPY --from` line above supplies the exact `tofu` version this repo already targets, instead of whatever Atlantis's own image happens to bundle.

Build it on the runner, from the repo clone:

```bash
cd /opt/infra/infrastructure
docker build -t infra-atlantis:latest -f services/runner-image/Dockerfile.atlantis services/runner-image
```

## 3. Run Atlantis

### 3.1 Create a dedicated GitHub token, not a reused one

Use a token created specifically for Atlantis, not one already in use elsewhere (e.g. the GHCR pull token from `concertfinder`'s own hosting doc). A dedicated token limits blast radius if it ever leaks, can be revoked on its own without disrupting anything else, and (per Atlantis's own documentation) makes it obvious in PR comments that they came from the automation, not from you acting manually.

**Fine-grained, repo-scoped token** (recommended default here):

1. On GitHub: **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Token name**: something identifiable, e.g. `atlantis-infrastructure`.
3. **Expiration**: since `sancheza/infrastructure` is a personal repo, "No expiration" is a selectable option (GitHub caps this at 366 days by default only for organization-owned repos, not personal ones). Available doesn't mean advisable: a dated expiration forces periodic rotation, which is worth having even in a single-operator setup, so a duration (with a reminder to rotate before it lapses) is still the better default; pick "No expiration" only if you're deliberately trading that off for convenience.
4. **Resource owner**: your account (`sancheza`).
5. **Repository access**: "Only select repositories" → `sancheza/infrastructure`.
6. **Permissions** (per [Atlantis's own access-credentials docs](https://www.runatlantis.io/docs/access-credentials)), under "Repository permissions":
   - Contents: **Read-only**
   - Commit statuses: **Read and write**
   - Pull requests: **Read and write**
   - Metadata: read-only (selected automatically once any other permission is set)
7. **Generate token**, then copy the value immediately: GitHub only shows it once.

This is narrower than a classic token's blanket `repo` scope (which grants access to every repo the account can see), so prefer it unless you hit the caveat below.

**Known limitation**: fine-grained tokens have a documented gap with Atlantis's mergeability/branch-protection checks, specifically when `atlantis.yaml` sets `apply_requirements` like `approved` or `undiverged`. The config in §5 doesn't use `apply_requirements` (applies are triggered manually via PR comment instead), so this shouldn't bite here. If you add that gate later and Atlantis starts failing to fetch PR status, switch this token to a classic PAT (`repo` scope) or a GitHub App instead.

**Going further** (optional): Atlantis's own docs recommend a dedicated bot GitHub account (e.g. a second free account named something like `atlantis-bot`) over any token on your personal account, specifically to keep automated PR comments visually distinct from your own. Worth doing if PR-comment clarity matters to you; a repo-scoped token on your existing account (above) is the pragmatic floor for a single-operator setup.

**Where to keep a copy of this token**: `.env` (§3.2) is the live copy Atlantis actually reads, but keep a durable record of the value somewhere else too. A password manager is the primary place: it's the one location that survives even if the runner's disk is wiped. Once Vault itself is up, writing a second copy into its KV v2 engine (e.g. `secret/bootstrap/atlantis-gh-token`) is also worth doing, as a backup/audit record, **not** as something Atlantis reads from at startup: Vault is what this pipeline provisions, so if Atlantis's own credential only lived in Vault, a sealed Vault (after any reboot or bad apply) would block Atlantis from fetching the very token it needs to fix Vault. Keep the runtime copy in `.env`, independent of whether Vault happens to be reachable.

### 3.2 Configure and start it

Atlantis's config lives in [services/runner-image/docker-compose.yml](runner-image/docker-compose.yml) (tracked) and a `.env` file (gitignored, real values, same split as `terraform.tfvars`/`terraform.tfvars.example`). This avoids retyping a long `docker run` command by hand every time the image is rebuilt or a value changes: `--restart unless-stopped` means you run this once at initial setup, but every later change to the image or its config still means recreating the container, which `docker compose up -d` does idempotently from the tracked files instead of a hand-typed command.

```bash
mkdir -p /opt/infra/atlantis-data
cd /opt/infra/infrastructure/services/runner-image
cp .env.example .env
chmod 600 .env
```

Edit `.env` with real values:

```bash
ATLANTIS_GH_USER=your-github-username
ATLANTIS_GH_TOKEN=the-fine-grained-token-from-3.1
ATLANTIS_GH_WEBHOOK_SECRET=generate-with-openssl-rand-hex-32
ATLANTIS_REPO_ALLOWLIST=github.com/sancheza/infrastructure
```

Remember to replace all four values in `.env`: `ATLANTIS_GH_USER` (the GitHub account that owns the token from §3.1, whether your own or a dedicated bot account), `ATLANTIS_GH_TOKEN` (the token itself), and `ATLANTIS_GH_WEBHOOK_SECRET` (generate one with `openssl rand -hex 32` and keep it; `ATLANTIS_REPO_ALLOWLIST` can usually stay as-is).

Then start it (still in `/opt/infra/infrastructure/services/runner-image`, where `docker-compose.yml` and `.env` both live):

```bash
docker compose up -d
```

**Whenever the image is rebuilt (§2) or `.env`/`docker-compose.yml` changes**, re-run that same command from this same directory:

```bash
cd /opt/infra/infrastructure/services/runner-image
docker compose up -d
```

This recreates the container from the current image and config, leaving `/opt/infra/atlantis-data` (Atlantis's own persistent state) untouched, the same update pattern already documented for other Docker-based services in this repo's ecosystem. §2's rebuild command runs from the repo root (`/opt/infra/infrastructure`), not this directory, so don't assume the `cd` carries over from there.

Plain bridge networking (no `--net=host`) is enough here: the runner's containers already reach the LAN (Proxmox API, Pi-hole, target hosts) over Docker's normal bridge, confirmed by testing directly rather than assumed. The compose file's `127.0.0.1:4141:4141` port binding is deliberate: Atlantis is reachable from this host only, never the LAN or internet. `gh webhook forward` (§4) is the only thing that talks to it. `/opt/infra/atlantis-data` is Atlantis's own persistent state (its BoltDB lock/PR database and its per-project ephemeral clones): back this up, or at least know it's there. Losing it loses in-flight PR lock state, not your actual infrastructure.

## 4. Deliver webhooks with `gh webhook forward`, not a public endpoint

Install the GitHub CLI and this extension on the runner LXC itself, not inside any Docker container: the systemd service below execs `/usr/bin/gh` directly as a host process, so `gh` has to exist there for it to find.

Install `gh` itself first (official apt repo, matching the same pattern already used for Docker's own repo in [opentofu_ansible_setup_guide.md](opentofu_ansible_setup_guide.md)):

```bash
(type -p wget >/dev/null || (apt update && apt install wget -y)) \
	&& mkdir -p -m 755 /etc/apt/keyrings \
	&& out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
	&& cat $out | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
	&& chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
	&& mkdir -p -m 755 /etc/apt/sources.list.d \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
	&& apt update \
	&& apt install gh -y
```

Then the extension and auth:

```bash
gh extension install cli/gh-webhook
gh auth login --hostname github.com --scopes admin:repo_hook
```

`gh auth login` (not `gh auth refresh`, which only modifies an *existing* authenticated session) is the actual first login on a fresh `gh` install. This LXC has no browser, but the device-flow login still works headless: the command prints a one-time code and a URL, which you open and enter on any other device (your phone, your laptop) to complete the login; the runner's own `gh` session picks it up once you do.

Run it as a systemd service so it survives reboots and reconnects on its own:

```bash
sudo tee /etc/systemd/system/atlantis-webhook-forward.service > /dev/null <<'EOF'
[Unit]
Description=Forward GitHub webhooks to local Atlantis
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/gh webhook forward --repo sancheza/infrastructure --events pull_request,pull_request_review,issue_comment,push --url http://127.0.0.1:4141/events
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now atlantis-webhook-forward
sudo systemctl status atlantis-webhook-forward
```

**Verify** by watching both ends: `journalctl -u atlantis-webhook-forward -f` on the runner, then open (or comment on) a PR against the repo and confirm an event shows up in that log and in `docker logs -f atlantis`.

**Known limitation, accepted deliberately:** GitHub documents webhook forwarding as a testing feature, and it enforces one forwarder per repo at a time: a second instance trying to start gets `Hook already exists`. Nothing here technically prevents the long-running use above, but there's no official production SLA behind it. If this ever proves unreliable, the documented fallback is a self-hosted GitHub Actions runner that relays the same events to Atlantis's `/events` endpoint instead: a fully-supported GitHub feature, at the cost of a bit more setup (register a runner, write a one-step relay workflow).

## 5. atlantis.yaml: one config that survives the pending repo-structure decision

`vault-provision` is being generalized into the template for future services, and whether that lands as a shared Terraform module or a copyable-per-service `main.tf` is still an open decision elsewhere in this repo. This config doesn't need that decided first: `autodiscover` finds any directory containing `.tf` files without you having to list each `services/<name>-provision/` by hand.

```yaml
# atlantis.yaml, repo root
version: 3
automerge: false

autodiscover:
  mode: auto

workflows:
  lxc-instance:
    plan:
      steps:
        - init
        - plan
    apply:
      steps:
        - apply
        - run: ansible-playbook deploy_*.yml
```

The `apply` workflow's `run:` step is a plain process invocation, not a `docker run`: `ansible-playbook` is already on `PATH` inside Atlantis's own container (§2), so this needs nothing more than the command itself.

Bind each project to this workflow, either per-project in this same file once you know the final directory layout, or, if `autodiscover` alone is enough for your case, via server-side default workflow config (`--default-workflow=lxc-instance` roughly captures the idea; check `atlantis server --help` against your installed version, since server-config flags shift between releases).

Check `atlantis.yaml` into the repo root and push. Atlantis picks it up automatically on the next event for that repo, no restart needed.

## 6. Day-to-day: how a change actually ships now

1. Edit `main.tf` / `terraform.tfvars.example` / `deploy_<service>.yml` on a branch, push, open a PR.
2. Atlantis comments the `tofu plan` output on the PR automatically.
3. Review the plan in the PR comment: this is the review gate that replaced eyeballing `tofu plan` output in a terminal.
4. Comment `atlantis apply` on the PR to apply, or just merge (`automerge` is `false` above, so applying is a deliberate second step, not automatic on merge; flip that once you trust the pipeline enough to want it).
5. Atlantis runs `tofu apply`, then the `ansible-playbook` step, and comments the result.

No SSH to the runner, no manual `git pull`, for any of this.

## 7. Known gap, deliberately deferred: `terraform.tfvars`

Atlantis clones this repo into its **own** ephemeral workspace per PR/project (under `/opt/infra/atlantis-data/repos/...`), not the long-lived `/opt/infra/infrastructure` checkout used for manual runs. Since `terraform.tfvars` is gitignored (by design: it holds real credentials), it does not exist in that ephemeral clone, and `tofu plan` will fail on missing required variables the first time this runs.

**This is intentionally left as a to-do, not solved here.** The immediate, unblocking stopgap once you're ready to test this end-to-end: an Atlantis [`pre_workflow_hook`](https://www.runatlantis.io/docs/pre-workflow-hooks) that copies a real tfvars file from a fixed path on `/opt/infra/atlantis-data` (outside any repo clone) into the ephemeral workspace before `plan` runs, e.g.:

```yaml
# server-side repo config, not atlantis.yaml
pre-workflow-hooks:
  - run: cp /atlantis/secrets/vault-provision.tfvars terraform.tfvars
```

Remember to replace `vault-provision.tfvars` with the actual filename you place at `/atlantis/secrets/` on the Atlantis host, and add one `run:` line per service workspace as more are added.

That still means a real secrets file sitting on the Atlantis host, placed there by hand once: no different in kind from today's manual setup, just relocated. The actual follow-up (tracked separately, not part of this document): move off plaintext `tfvars` entirely, toward `TF_VAR_*` environment variables injected server-side or, longer-term, Vault-issued secrets once Vault itself is stood up. Fitting, since this pipeline is what provisions Vault in the first place.

## 8. Troubleshooting

**Atlantis never comments on a PR**: confirm `atlantis-webhook-forward` is actually running (`systemctl status`) and check its log for delivery errors before assuming Atlantis itself is broken. Most failures at this stage are the forwarder, not Atlantis.

**`gh webhook forward` exits with `Hook already exists`**: another forwarder (or a stale one from a prior run) already registered against this repo. `gh api -X GET /repos/sancheza/infrastructure/hooks` lists them; delete the stale one and restart the service.

**Plan fails with a missing-variable error**: expected until §7's stopgap (or its real fix) is in place. This isn't a bug in the Atlantis setup itself.
