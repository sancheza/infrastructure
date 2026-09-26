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

**`USER atlantis` (the last line above) has a real consequence worth knowing before it bites you: everything Atlantis runs is non-root, UID 100.** `run.sh`'s `infra-runner` containers run as root, so they can read anything under `/root` on the host without a second thought. Atlantis's container can't: `/root` itself is mode `700`, owned by `root:root`, in the base image, so UID 100 can't even traverse into it, regardless of what's bind-mounted underneath. Confirmed the hard way: mounting a properly-owned copy of the orchestration SSH key at `/root/.ssh` still failed with `Permission denied`, because the block was `/root` itself, not the key's own permissions. The fix used throughout this pipeline: never mount anything Atlantis needs at a path under `/root`. Two neutral, top-level mount points carry everything both execution paths (`run.sh` and Atlantis) need to share:

- **`/keys`**: the orchestration SSH key, owned to match whichever container reads it (root for `run.sh`, UID 100/GID 1000 for Atlantis).
- **`/secrets`**: anything a playbook needs to persist *across* separate runs, not just within one: `terraform.tfvars` for the `pre_workflow_hook` (§5), and any output a playbook writes for a later run to read back (§7 covers a real example: `vault-cluster-keys.json`).

The same non-root constraint is also why `deploy_vault.yml`'s "Save unseal keys" task writes to `/secrets/vault-cluster-keys.json` rather than `{{ playbook_dir }}`-relative: `playbook_dir` is a fresh, disposable path inside whichever ephemeral clone happens to be running (§5 explains why Atlantis's clones are ephemeral per PR), readable by the PR run that wrote it, but gone by the time a *different* PR needs to read it back. A fixed, shared path is required for anything that must outlive a single PR's clone, independent of which user is reading it.

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

### 3.3 Verify it's running

Three independent checks, from least to most revealing. Each one narrows down where a problem is if a later one fails.

**Container itself, no PR needed:**

```bash
docker ps --filter name=atlantis
docker logs atlantis --tail 20
```

Look for `Atlantis started - listening on port 4141` in the logs, and a `STATUS` that isn't `Restarting` in `docker ps`. `(health: starting)` for the first several minutes is normal, not a fault: the base image's healthcheck runs every 5 minutes with no shorter initial check, so `docker ps` won't show `(healthy)` until the first one actually fires. To confirm the check itself would pass right now, without waiting:

```bash
docker exec atlantis sh -c 'curl -f http://localhost:4141/healthz'
```

**HTTP health check, still no PR needed** (run on the runner itself; the port is loopback-only, see above):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4141/healthz
```

`200` means Atlantis is actually serving requests, not just that the process is running.

**There is a web UI**, served at `/` on the same port: a status page listing recent plans/applies and any held locks. Since the port is deliberately bound to `127.0.0.1` only (not the LAN, not the internet), reaching it from your own machine needs an SSH tunnel, not a direct browser connection to the runner:

```bash
ssh -L 4141:127.0.0.1:4141 root@opentofu
```

Then open `http://127.0.0.1:4141/` in a browser on your own machine, for as long as that SSH session stays open. This is read-only visibility; it doesn't change how PRs actually get planned or applied, that's still driven entirely by GitHub events through §4.

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

`gh auth login` (not `gh auth refresh`, which only modifies an *existing* authenticated session) is the actual first login on a fresh `gh` install. It's interactive; answer its prompts exactly like this:

1. **"What is your preferred protocol for Git operations on this host?"** → `HTTPS` (either works for what this guide needs `gh` for; HTTPS avoids also having to set up a separate SSH key for `gh` itself).
2. **"Authenticate Git with your GitHub credentials?"** → `Yes`.
3. **"How would you like to authenticate GitHub CLI?"** → **`Login with a web browser`**, not "Paste an authentication token". The token-paste option requires you to have already generated your own classic PAT (with `repo`, `read:org`, `workflow` scopes) somewhere else first, a separate manual step this guide doesn't otherwise need; picking the web-browser option instead has `gh` handle that negotiation itself, and is what actually produces the "log in from another device" flow below.

Once you pick "Login with a web browser", `gh` prints a one-time code and a URL: open that URL on any other device (your phone, your laptop) and enter the code there to complete the login. The runner's own `gh` session picks it up as soon as you do, no browser needed on the LXC itself.

Run it as a systemd service so it survives reboots and reconnects on its own:

```bash
sudo tee /etc/systemd/system/atlantis-webhook-forward.service > /dev/null <<'EOF'
[Unit]
Description=Forward GitHub webhooks to local Atlantis
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=root
Environment=HOME=/root
EnvironmentFile=/opt/infra/infrastructure/services/runner-image/.env
ExecStart=/usr/bin/gh webhook forward --repo sancheza/infrastructure --events pull_request,pull_request_review,issue_comment,push --url http://127.0.0.1:4141/events --secret ${ATLANTIS_GH_WEBHOOK_SECRET}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now atlantis-webhook-forward
sudo systemctl status atlantis-webhook-forward
```

**`--secret ${ATLANTIS_GH_WEBHOOK_SECRET}` is required, not optional.** Without it, `gh webhook forward` creates its GitHub-side webhook with no secret at all, so Atlantis's own signature check (`ATLANTIS_GH_WEBHOOK_SECRET` in `.env`) rejects every event with `missing signature`, including GitHub's automatic `ping` event the moment the webhook is created. `EnvironmentFile=` points at the same `.env` Atlantis itself reads (§3.2), so there's exactly one place this secret is set, not two copies to keep in sync.

**`User=root` and `Environment=HOME=/root` are both required, not optional.** Systemd system units with no explicit `User=` still run as root, but don't export `$HOME` into the process's environment the way an interactive root login shell does. `gh` resolves both its installed extensions (`~/.local/share/gh/extensions/`) and its stored auth token (`~/.config/gh/hosts.yml`, from §4's `gh auth login`) relative to `$HOME`. Without it set explicitly here, the service can't find either, even though both exist exactly where §4 put them. Symptom without this: the unit crash-loops, and `journalctl` shows `gh webhook is available as an official extension. To install it, run: gh extension install cli/gh-webhook`, even though it's already installed.

**Verify** by watching both ends: `journalctl -u atlantis-webhook-forward -f` on the runner, then open (or comment on) a PR against the repo and confirm an event shows up in that log and in `docker logs -f atlantis`.

**Known limitation, accepted deliberately:** GitHub documents webhook forwarding as a testing feature, and it enforces one forwarder per repo at a time: a second instance trying to start gets `Hook already exists`. Nothing here technically prevents the long-running use above, but there's no official production SLA behind it. If this ever proves unreliable, the documented fallback is a self-hosted GitHub Actions runner that relays the same events to Atlantis's `/events` endpoint instead: a fully-supported GitHub feature, at the cost of a bit more setup (register a runner, write a one-step relay workflow).

## 5. atlantis.yaml: one config that survives the pending repo-structure decision

`vault-provision` is being generalized into the template for future services, and whether that lands as a shared Terraform module or a copyable-per-service `main.tf` is still an open decision elsewhere in this repo. This config doesn't need that decided first.

**`autodiscover` alone does not bind a project to a custom workflow.** The first version of this section claimed it did; a real end-to-end test proved otherwise. `autodiscover: mode: enabled` still finds every `.tf`-containing directory automatically (so you never have to tell Atlantis a new service directory *exists*), but a discovered project with no explicit `workflow:` runs Atlantis's own built-in default workflow, silently, with no error: `tofu apply` succeeds and the whole `run: ansible-playbook ...` step just never happens. The fix, confirmed working: an explicit `projects:` entry naming the workflow, one per service.

```yaml
# atlantis.yaml, repo root
version: 3
automerge: false

autodiscover:
  mode: enabled

projects:
- dir: services/vault-provision
  workflow: lxc-instance

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

`autodiscover.mode: enabled` (not `auto`) matters here specifically because an explicit `projects:` entry now exists: `auto` mode only autodiscovers when *no* projects are configured at all, so once you add one explicit project (as above), `auto` would stop autodiscovering anything else. `enabled` keeps autodiscovering every other directory unconditionally, while the explicit entry above takes precedence for `services/vault-provision` specifically. **Every new service needs one more entry under `projects:`** naming its own directory (same `workflow: lxc-instance` value works for all of them; the workflow itself doesn't need repeating).

The `apply` workflow's `run:` step is a plain process invocation, not a `docker run`: `ansible-playbook` is already on `PATH` inside Atlantis's own container (§2), so this needs nothing more than the command itself.

**Two separate server-side permissions are required, not one.** A repo-level `atlantis.yaml` defining a custom `workflows:` block is rejected by default (`repo config not allowed to define custom workflows: server-side config needs 'allow_custom_workflows: true'`), and separately, a project setting an explicit `workflow:` key is *also* rejected by default (`repo config not allowed to set 'workflow' key: server-side config needs 'allowed_overrides: [workflow]'`). Both were hit for real, in that order, against this exact config. Custom workflows run arbitrary shell commands, so Atlantis requires the server operator to opt a repo into each capability separately from whatever the repo itself commits. Since the server operator and repo owner are the same person here, that's just a config file to add, not a real barrier: [services/runner-image/repos.yaml](runner-image/repos.yaml) (tracked, mounted into Atlantis at `/atlantis-config/repos.yaml`, referenced via `ATLANTIS_REPO_CONFIG` in `docker-compose.yml`).

```yaml
# services/runner-image/repos.yaml
repos:
- id: github.com/sancheza/infrastructure
  allow_custom_workflows: true
  allowed_overrides: [workflow]
  pre_workflow_hooks:
    - run: cp /atlantis/secrets/vault-provision.tfvars $DIR/services/vault-provision/terraform.tfvars
      description: Supply terraform.tfvars for vault-provision (gitignored, not in the clone)
```

(§7 covers what that `pre_workflow_hooks` entry is for and why it's shaped this way.)

Check `atlantis.yaml` (repo root), `repos.yaml`, and `docker-compose.yml` into git and push. `atlantis.yaml` itself needs no restart, Atlantis reads it fresh on every event; `repos.yaml` and `docker-compose.yml` do need one, since they're loaded at container startup (`cd .../services/runner-image && docker compose up -d`, §3.2, or `docker restart atlantis` if only `repos.yaml`'s *content* changed and compose doesn't detect anything to recreate).

## 6. Day-to-day: how a change actually ships now

1. Edit `main.tf` / `terraform.tfvars.example` / `deploy_<service>.yml` on a branch, push, open a PR.
2. Atlantis comments the `tofu plan` output on the PR automatically.
3. Review the plan in the PR comment: this is the review gate that replaced eyeballing `tofu plan` output in a terminal.
4. Comment `atlantis apply` on the PR to apply, or just merge (`automerge` is `false` above, so applying is a deliberate second step, not automatic on merge; flip that once you trust the pipeline enough to want it).
5. Atlantis runs `tofu apply`, then the `ansible-playbook` step, and comments the result.

No SSH to the runner, no manual `git pull`, for any of this.

## 7. `terraform.tfvars` in an ephemeral clone: resolved, not deferred

Atlantis clones this repo into its **own** ephemeral workspace per PR/project (`/atlantis/repos/...` inside its container), not the long-lived `/opt/infra/infrastructure` checkout used for manual runs. Since `terraform.tfvars` is gitignored (by design: it holds real credentials), it does not exist in that ephemeral clone on its own, and `tofu plan` fails on missing required variables without help. Confirmed by a real test PR reaching exactly that failure (`No value for required variable` for `pve_endpoint`, `pve_api_token`, and every other variable `terraform.tfvars` would supply) before this section's fix existed.

**The fix, verified working end-to-end**: the `pre_workflow_hooks` entry already shown in §5, supplying the file from a fixed path before `plan`/`apply` run:

```yaml
pre_workflow_hooks:
  - run: cp /atlantis/secrets/vault-provision.tfvars $DIR/services/vault-provision/terraform.tfvars
    description: Supply terraform.tfvars for vault-provision (gitignored, not in the clone)
```

`$DIR` is Atlantis's own environment variable for "the absolute path to the root of the cloned repository" (documented, not guessed), which is why the destination is `$DIR/services/vault-provision/...` rather than a bare relative filename: `pre_workflow_hooks` run once per PR at the repo root, before any specific project's workflow, not inside a project's own directory. **Add one more `run:` line, with its own destination path, per service** as more are added; there's no way to write a single glob-style line that covers every service's tfvars at once, since each one needs a different real file.

The source file itself, `/atlantis/secrets/vault-provision.tfvars`, is a real secrets file placed on the Atlantis host by hand once (`/opt/infra/atlantis-data/secrets/vault-provision.tfvars` on the host, `chown`'d to match Atlantis's non-root UID per the note in §2, `chmod 600`): no different in kind from today's manual setup, just relocated to a place every PR's ephemeral clone can reach. The actual follow-up (tracked separately, not part of this document): move off plaintext `tfvars` entirely, toward `TF_VAR_*` environment variables injected server-side or, longer-term, Vault-issued secrets once Vault itself is stood up. Fitting, since this pipeline is what provisions Vault in the first place.

## 8. Troubleshooting

**Atlantis never comments on a PR**: confirm `atlantis-webhook-forward` is actually running (`systemctl status`) and check its log for delivery errors before assuming Atlantis itself is broken. Most failures at this stage are the forwarder, not Atlantis.

**`gh webhook forward` exits with `Hook already exists`**: another forwarder (or a stale one from a prior run) already registered against this repo. `gh api -X GET /repos/sancheza/infrastructure/hooks` lists them; delete the stale one and restart the service.

**Plan fails with a missing-variable error** (`No value for required variable`): §7's `pre_workflow_hooks` isn't running, isn't configured for this project's directory, or `repos.yaml` hasn't been picked up yet (it loads at container startup; `docker restart atlantis` after any `repos.yaml` change, per §5). Check the PR's own checks for a separate `pre_workflow_hook` status entry: it reports success/failure independently of `plan`, and a failed hook still lets `plan` run anyway (`fail-on-pre-workflow-hook-error` isn't set), producing exactly this downstream error with the real cause one check up.

**`repo config not allowed to define custom workflows`**: `allow_custom_workflows: true` missing from this repo's entry in `repos.yaml` (§5). Requires a container restart to take effect, not just a file edit.

**`repo config not allowed to set 'workflow' key`**: `allowed_overrides: [workflow]` missing from the same `repos.yaml` entry (§5), a separate permission from `allow_custom_workflows` above; both are required together for an explicit `projects:` entry to bind a workflow.

**`Error acquiring the state lock` / `open /tfstate/....tfstate: permission denied`, or an Ansible task failing with `no such identity: ...: Permission denied`**: a host directory mounted into Atlantis isn't owned to match its container's UID/GID. Check with `docker exec atlantis id` (expect `uid=100(atlantis) gid=1000(atlantis)`), then `chown -R 100:1000` whichever host directory the failing mount points at (`/opt/infra/tfstate`, `/opt/infra/atlantis-ssh`, `/opt/infra/atlantis-data/secrets`). See §2's note on why this bites specifically for anything mounted under `/root`.

**An Ansible task fails with `Unable to access the file '/secrets/....json'`**: either a *different* project's PR wrote that file to a `playbook_dir`-relative path instead of `/secrets` (an older version of `deploy_<service>.yml`, or a custom copy that didn't follow §2's guidance), or the service has never successfully completed a full init before, so the file has never been written yet, which is expected. Check `deploy_<service>.yml`'s own read/write tasks for this file both use the fixed `/secrets/...` path, not `playbook_dir`.
