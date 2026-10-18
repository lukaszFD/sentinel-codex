# Sentinel Codex

Personal knowledge base built on top of the Cyber Sentinel server. It does not
change Cyber Sentinel; it uses its Postgres, n8n, Vault and nginx-proxy as a
platform and keeps its own database, Vault space, n8n credentials, containers
and deployment.

Modules:

| Module | What it does |
|---|---|
| `news` — News Radar | Collects AI and cyber news (Google News, Hacker News, RSS, CISA KEV), filters by keywords, scores with Gemini, sends an e-mail digest at set times (default 08:00, 13:00, 18:00). Config page: `codex-news.<suffix>`. |

## Layout

```
hub/postgres/00_knowledge_hub.sql   database knowledge_hub (no module objects)
modules/<module>/db/                schema + roles of the module
modules/<module>/n8n/               workflow JSON + its Code-node sources (src/)
modules/<module>/ui/                config page (Flask), built by compose
docker/docker-compose-sentinel-codex.yml
ansible/                            01 database, 02 Vault, 03 n8n, 04 config pages
```

Database `knowledge_hub` holds one schema per module. Each module has its own
roles (`<module>_n8n`, `<module>_editor`) that see only their schema. Cyber
Sentinel roles cannot connect to it.

## Secrets

Same flow as Cyber Sentinel, in Codex's own space:

```
ansible-vault (vault.yml) ──02──> HashiCorp Vault  secret/sentinel-codex/*
                          └─03──> n8n credentials  Codex Vault, Codex SMTP, Codex news (Postgres)
```

| Vault path (`secret/sentinel-codex/…`) | Read by |
|---|---|
| `api-keys/gemini` | workflow, at run time |
| `credentials/gmail` (user, password, to) | workflow (sender/recipient); password also in "Codex SMTP" |
| `credentials/postgres/news_n8n`, `news_editor` | reference copy; n8n and the page get them from credential / `.env` |

n8n reads Vault with its own token (rotated on every deploy), policy
`sentinel-codex-n8n`, read-only on
`api-keys/*` and `credentials/gmail`. It cannot read Cyber Sentinel's paths,
and Cyber Sentinel's token cannot read these. Postgres and SMTP nodes in n8n
only take passwords from n8n credentials, so playbook 03 writes the same
ansible-vault value there.

Gemini key: from a separate Google AI Studio project. Limits apply per
project, so a key in Cyber Sentinel's project would share its quota.

## What it uses from Cyber Sentinel

All names are in `ansible/group_vars/all/codex.yml` (`[VARS.PLATFORM]`).
Every playbook checks they exist before doing anything.

| Platform part | Used for |
|---|---|
| container `postgres_db` | hosts database `knowledge_hub` |
| container `n8n-server` | runs the workflows (with Codex's own credentials) |
| HashiCorp Vault (`127.0.0.1:8200` from the host, `10.10.10.12:8200` from n8n) | Codex secrets under `secret/sentinel-codex/`; Ansible uses the same root token as Cyber Sentinel |
| network `cyber-sentinel_internal_network` | Codex containers join it; IPs 10.10.10.20–29 are Codex's |
| container `nginx-proxy` + `~/config/nginx/{conf.d,certs}` | Codex adds `codex_*` files there and reloads nginx |

If Cyber Sentinel is down, Codex is down too.

## Deploy

1. `cp ansible/group_vars/all/vault.yml.example ansible/group_vars/all/vault.yml`,
   fill in, `ansible-vault encrypt` it, put the password in `ansible/.vault_pass`.
   If the Vault root token differs between dev and prod, put it in
   `ansible/host_vars/<host>/vault.yml`.
2. Add `codex-news.prod` / `codex-news.local` to Pi-hole local DNS.
3. Dev first:

   ```
   cd ansible
   ansible-playbook 00_main.yml --limit vm-prox-dev
   ```

4. Activate the "News Radar" workflow in n8n.

n8n's Vault token is rotated on every deploy (new one minted, old Codex
tokens revoked), so there is nothing to save. It is valid for a year —
deploy at least once a year. Playbooks 02 and 03 always run together.

Re-running is safe: SQL keeps tuned settings and edited rows, the workflow is
imported only if it is not in n8n yet.

## Changing the workflow

Edit `modules/news/n8n/src/*.js` or the node layout in
`modules/news/n8n/build_workflow.py`, run `python3 build_workflow.py`, commit
both. A workflow already in n8n is not replaced by playbook 03 unless you ask:

```
ansible-playbook 02_vault.yml 03_n8n.yml --limit vm-prox-dev -e codex_workflow_reimport=true
```

This overwrites edits made in n8n and leaves the workflow inactive — activate
it again.

## Adding a module

1. `modules/<name>/db/NN_<name>.sql`: schema `<name>`, its roles, grants,
   `GRANT CONNECT ON DATABASE knowledge_hub` to those roles.
2. Add it to playbook 01 (render + run + privilege check), its secrets and
   policy paths to 02, its n8n credential and workflow to 03, its page to the
   compose file and 04.
3. Variables and vault keys next to the `news` ones.
