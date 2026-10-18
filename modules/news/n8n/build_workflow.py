"""Builds "News Radar.json" from the Code-node sources in src/.

Edit the JavaScript in src/ (or the node layout below), then run
    python3 build_workflow.py
and commit both. Node ids are derived from node names, so a rebuild
produces the same JSON when nothing changed.

Credentials: all are Codex's own, created by ansible playbook 03 with fixed
ids (group_vars/all/codex.yml). Secrets read at run time come from Vault
(secret/sentinel-codex/*, read-only token, playbook 02): the Gemini key and
the digest sender/recipient. No Cyber Sentinel credential is used.
"""
import json
import uuid
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "src"
OUT = HERE / "News Radar.json"

# Must match ansible/group_vars/all/codex.yml (news_n8n_credential_*).
PG = {"postgres": {"id": "cdxNewsPostgres1", "name": "Codex news (Postgres)"}}
VAULT = {"hashiCorpVaultApi": {"id": "cdxVaultToken001", "name": "Codex Vault"}}
SMTP = {"smtp": {"id": "cdxSmtpGmail0001", "name": "Codex SMTP"}}


def nid(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "news-radar/" + name))


def code(name, file, pos, mode="runOnceForAllItems"):
    p = {"jsCode": (SRC / file).read_text()}
    if mode != "runOnceForAllItems":
        p = {"mode": mode, **p}
    return {"parameters": p, "type": "n8n-nodes-base.code", "typeVersion": 2,
            "position": pos, "id": nid(name), "name": name}


def pg(name, query, pos, replacement=None, **extra):
    params = {"operation": "executeQuery", "query": query, "options": {}}
    if replacement:
        params["options"]["queryReplacement"] = replacement
    return {"parameters": params, "type": "n8n-nodes-base.postgres", "typeVersion": 2.6,
            "position": pos, "id": nid(name), "name": name, "credentials": PG, **extra}


def sticky(name, text, pos, w, h, color=None):
    p = {"content": text, "height": h, "width": w}
    if color:
        p["color"] = color
    return {"parameters": p, "type": "n8n-nodes-base.stickyNote", "typeVersion": 1,
            "position": pos, "id": nid(name), "name": name}


Y0, Y1, Y2, Y3 = 0, 260, 520, 800

nodes = [
    {"parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}},
     "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [-600, Y1],
     "id": nid("Every 15 minutes"), "name": "Every 15 minutes"},

    # Branch 0 — housekeeping
    pg("Housekeeping",
       "-- Retire items that used up ai_max_attempts, apply retention_days.\n"
       "SELECT news.sp_housekeeping();",
       [-300, Y0]),

    # Branch 1 — fetch + ingest
    pg("Due sources",
       "-- Active sources whose poll_minutes has passed (empty when enabled = 0).\n"
       "SELECT * FROM news.v_due_sources;",
       [-300, Y1]),
    code("Build fetch request", "build_fetch_request.js", [-60, Y1]),
    {"parameters": {
        "url": "={{ $json.fetch_url }}",
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "User-Agent", "value": "Mozilla/5.0 (compatible; CyberSentinel-NewsRadar/1.0)"},
            {"name": "Accept", "value": "application/rss+xml, application/atom+xml, application/xml, application/json;q=0.9, */*;q=0.8"},
        ]},
        "options": {
            "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "text"}},
            "timeout": 20000,
        }},
     "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [180, Y1],
     "id": nid("Fetch source"), "name": "Fetch source", "onError": "continueRegularOutput"},
    code("Parse feed", "parse_feed.js", [420, Y1], mode="runOnceForEachItem"),
    pg("Record fetch",
       "-- New items get a status (stale / duplicate / filtered / pending_ai / alert),\n"
       "-- known URLs are ignored. A non-empty error marks the source as failed.\n"
       "SELECT $1::int AS source_id,\n"
       "       news.sp_record_fetch($1::int, $2::jsonb, NULLIF($3, '')) AS result;",
       [660, Y1],
       "={{ [ $json.source_id, JSON.stringify($json.items), $json.error ] }}"),

    # Branch 2 — AI scoring
    pg("Claim AI batch",
       "-- Up to ai_batch_size pending items + the active prompt.\n"
       "SELECT c.*, p.version AS prompt_version, p.system_prompt\n"
       "FROM news.sp_claim_for_ai() c\n"
       "CROSS JOIN (SELECT version, system_prompt\n"
       "            FROM news.prompts WHERE is_active) p;",
       [-300, Y2]),
    code("Build Gemini request", "build_gemini_request.js", [-60, Y2]),
    # Runs only when there is a batch (Build Gemini request returns nothing otherwise).
    {"parameters": {"secretPath": "sentinel-codex/api-keys/gemini", "additionalFields": {}},
     "type": "n8n-nodes-hashi-vault.hashiCorpVault", "typeVersion": 1, "position": [160, Y2],
     "id": nid("Gemini API key"), "name": "Gemini API key", "credentials": VAULT},
    {"parameters": {
        "method": "POST",
        "url": "=https://generativelanguage.googleapis.com/v1beta/models/{{ $('Build Gemini request').first().json.model }}:generateContent",
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "x-goog-api-key", "value": "={{ $json.data.token }}"},
        ]},
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify($('Build Gemini request').first().json.body) }}",
        "options": {
            "response": {"response": {"fullResponse": True, "neverError": True}},
            "timeout": 90000,
        }},
     "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [380, Y2],
     "id": nid("Gemini score batch"), "name": "Gemini score batch",
     "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 5000, "onError": "continueRegularOutput"},
    code("Parse verdicts", "parse_verdicts.js", [600, Y2]),
    pg("Store verdicts",
       "-- score >= digest_min_ai_score => alert (next digest), else scored.\n"
       "SELECT news.sp_store_verdicts($1::jsonb, $2, $3) AS stored,\n"
       "       $4::int AS claimed, NULLIF($5, '') AS error;",
       [820, Y2],
       "={{ [ JSON.stringify($json.verdicts), $json.model, $json.prompt_version, $json.claimed, $json.error ] }}"),

    # Branch 3 — digest
    pg("Digest due?",
       "-- Nothing unless a slot from digest_slots is due; sp_digest_begin()\n"
       "-- claims the slot so it is sent once. One row per digest item, or one row\n"
       "-- with only digest_id when nothing qualifies.\n"
       "WITH d AS MATERIALIZED (\n"
       "    SELECT news.sp_digest_begin() AS digest_id\n"
       ")\n"
       "SELECT d.digest_id, i.id, i.source, i.topic, i.title, i.url, i.published_at,\n"
       "       i.ai_score, i.ai_summary_pl, i.keyword_score, i.hashtags,\n"
       "       news.setting('linkedin_share_enabled') = 1 AS share_enabled\n"
       "FROM d\n"
       "LEFT JOIN news.v_digest_items i ON TRUE\n"
       "WHERE d.digest_id IS NOT NULL;",
       [-300, Y3]),
    code("Build digest e-mail", "build_digest_email.js", [-60, Y3]),
    {"parameters": {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{
                "id": nid("cond-anything"),
                "leftValue": "={{ $json.empty === false }}",
                "rightValue": "",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "options": {}},
     "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [180, Y3],
     "id": nid("Anything to send?"), "name": "Anything to send?"},
    {"parameters": {"secretPath": "sentinel-codex/credentials/gmail", "additionalFields": {}},
     "type": "n8n-nodes-hashi-vault.hashiCorpVault", "typeVersion": 1, "position": [420, Y3 - 80],
     "id": nid("Get Email Pass"), "name": "Get Email Pass", "credentials": VAULT},
    {"parameters": {
        "fromEmail": "={{ $('Get Email Pass').item.json.data.user }}",
        "toEmail": "={{ $('Get Email Pass').item.json.data.to }}",
        "subject": "={{ $('Build digest e-mail').item.json.subject }}",
        "html": "={{ $('Build digest e-mail').item.json.html }}",
        "options": {}},
     "type": "n8n-nodes-base.emailSend", "typeVersion": 2.1, "position": [660, Y3 - 80],
     "id": nid("Send digest"), "name": "Send digest", "webhookId": nid("Send digest webhook"),
     "credentials": SMTP, "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 10000,
     "onError": "continueErrorOutput"},
    pg("Mark digest sent",
       "SELECT news.sp_digest_finish($1::bigint, $2::bigint[]);",
       [920, Y3 - 160],
       "={{ [ $('Build digest e-mail').item.json.digest_id, $('Build digest e-mail').item.json.ids ] }}"),
    pg("Mark digest failed",
       "-- Items stay 'alert'; the slot is retried on the next runs (max 3 attempts).\n"
       "SELECT news.sp_digest_finish($1::bigint, '{}'::bigint[], $2);",
       [920, Y3],
       "={{ [ $('Build digest e-mail').item.json.digest_id, String($json.error?.message ?? $json.error ?? 'e-mail send failed').slice(0, 500) ] }}"),
    pg("Mark digest empty",
       "SELECT news.sp_digest_finish($1::bigint, '{}'::bigint[]);",
       [420, Y3 + 120],
       "={{ [ $json.digest_id ] }}"),

    sticky("Note overview",
           "## News Radar\nAI + cyber news → Gemini score 1–5 → e-mail digest.\n\n"
           "Sentinel Codex, module `news`. All configuration lives in database "
           "`knowledge_hub`, schema `news`, edited on the **codex-news** page "
           "(sources, keywords, thresholds, digest times, prompt). Nothing to change here.\n\n"
           "Branches run top to bottom on every trigger; each one stops by itself when it has "
           "nothing to do.",
           [-680, -260], 340, 230, 4),
    sticky("Note fetch",
           "### 1 · Fetch\nOnly sources whose `poll_minutes` passed. A failed feed is recorded "
           "(`source_state`) and never stops the run.",
           [-340, Y1 - 120], 1120, 260),
    sticky("Note ai",
           "### 2 · AI score\nOne Gemini call per run (`ai_batch_size` items), key from Vault "
           "`sentinel-codex/api-keys/gemini`. Bad answer = items stay "
           "pending and are retried; after `ai_max_attempts` they become `ai_error`.\n"
           "Model: `MODEL` in *Build Gemini request*.",
           [-340, Y2 - 120], 1320, 260),
    sticky("Note digest",
           "### 3 · Digest\nSent on the first run after each active slot (08:00 / 13:00 / 18:00 "
           "Europe/Warsaw by default). A failed send is retried up to 3 times.",
           [-340, Y3 - 200], 1380, 400),
]


def link(*names):
    return {"main": [[{"node": n, "type": "main", "index": 0} for n in names]]}


connections = {
    "Every 15 minutes": link("Housekeeping", "Due sources", "Claim AI batch", "Digest due?"),
    "Due sources": link("Build fetch request"),
    "Build fetch request": link("Fetch source"),
    "Fetch source": link("Parse feed"),
    "Parse feed": link("Record fetch"),
    "Claim AI batch": link("Build Gemini request"),
    "Build Gemini request": link("Gemini API key"),
    "Gemini API key": link("Gemini score batch"),
    "Gemini score batch": link("Parse verdicts"),
    "Parse verdicts": link("Store verdicts"),
    "Digest due?": link("Build digest e-mail"),
    "Build digest e-mail": link("Anything to send?"),
    "Anything to send?": {"main": [
        [{"node": "Get Email Pass", "type": "main", "index": 0}],
        [{"node": "Mark digest empty", "type": "main", "index": 0}],
    ]},
    "Get Email Pass": link("Send digest"),
    "Send digest": {"main": [
        [{"node": "Mark digest sent", "type": "main", "index": 0}],
        [{"node": "Mark digest failed", "type": "main", "index": 0}],
    ]},
}

wf = {
    "name": "News Radar",
    "nodes": nodes,
    "pinData": {},
    "connections": connections,
    "active": False,
    "settings": {"executionOrder": "v1", "binaryMode": "separate", "timezone": "Europe/Warsaw"},
    "versionId": nid("version-1"),
    "meta": {"templateCredsSetupCompleted": True},
    "id": "cdxNewsRadar0001",
    "tags": [],
}

OUT.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
print(f"{len(nodes)} nodes -> {OUT.name}")
