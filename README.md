# AI Agency Agent (n8n + Ollama + Docker)

A chat web app where a local LLM works like an agent and uses **n8n** to run a small
automation agency for local businesses (restaurants, salons, gyms, clinics).
No paid API and no cloud account: everything runs in Docker (your machine or a free GitHub Codespace).

```
Browser chat  ->  Dashboard (Flask)  ->  Ollama (LLM with tool calling)
                       |                       
                       +--> client CRM (SQLite)
                       +--> n8n public API  -> creates + activates workflows
                       +--> n8n webhooks    -> tests them
```

## What the agent can do

Tell it in plain Bangla or English (Banglish works too):

- add a client and remember their business details
- deploy an automation to n8n for that client and return a ready webhook URL
- test the automation, switch it on or off, list everything it has built

Automation types (each becomes `Webhook -> Prepare prompt -> Ollama -> Respond to Webhook` in n8n,
and answers with a polite fallback message if the model is down):

| kind | use |
|---|---|
| `faq_chatbot` | answers customer questions and collects booking details (the monthly retainer product) |
| `review_reply` | drafts a polite reply to a Google review |
| `lead_followup` | writes a short WhatsApp/SMS follow-up for a new lead |
| `social_post` | writes a Facebook/Instagram caption (content upsell) |

Every deployed automation is called the same way:

```bash
curl -X POST "<webhook_url>" -H "Content-Type: application/json" \
     -d '{"message": "Do you open on Friday?"}'
# -> {"reply": "..."}
```

## Quick start

1. Copy the settings and change the passwords:
   ```bash
   cp .env.example .env
   ```
   Set at least `POSTGRES_PASSWORD`, `N8N_ENCRYPTION_KEY` and `DASHBOARD_PASSWORD`.

2. Start everything:
   ```bash
   docker compose up -d
   ```
   The first start downloads the model (`DEFAULT_MODEL`, about 2 GB for the default). Watch it with
   `docker compose logs -f ollama-pull`.

3. Open **n8n** at http://localhost:5678, create the owner account, then go to
   **Settings -> n8n API -> Create an API key**. Newer n8n versions ask for scopes: choose all, or at
   least the `workflow:*` ones (create, read, list, update, activate, deactivate).
   Put the key in `.env` as `N8N_API_KEY` and run:
   ```bash
   docker compose up -d dashboard
   ```

4. Open the **dashboard** at http://localhost:8080 (any username, the `DASHBOARD_PASSWORD` as password).
   The two badges at the top should say *Ollama ready* and *n8n ready*.

5. Try: `Add a salon client called Rupa Beauty. Open 10am-8pm, haircut 300 BDT.` then
   `Deploy an FAQ chatbot for client 1` then `Test it with: what time do you open?`

### GitHub Codespaces

Open the repo in a Codespace; the dev container starts all services. Create `.env` as above, then
run **Rebuild Container** so it is picked up. Set `WEBHOOK_URL` to the forwarded URL of port 5678
(`https://<codespace>-5678.app.github.dev/`) so webhook URLs shown by the agent are correct.
Forwarded ports are private by default; make port 5678 public only when a client must call a webhook.

## Honest limits

- **Small local models make mistakes.** `llama3.2:3b` fits a free Codespace (2 cores, no GPU) but
  sometimes picks the wrong tool or asks again. The agent returns tool errors to the model so it can
  recover, and every tool call is shown in the chat so you can check it. On a bigger machine set
  `DEFAULT_MODEL=llama3.1:8b` (or `qwen2.5:7b`) and run `docker compose up -d` again.
- **CPU answers are slow** (tens of seconds). Client-facing chatbots on CPU are fine for demos and
  low traffic, not for busy shops.
- Codespaces stops when idle and free hours are limited, so a permanent client deployment needs an
  always-on host. The same `docker-compose.yml` runs unchanged on any VPS or a free-tier VM.
- n8n runs `latest`; pin a version once you have clients so an update cannot break their workflows.
  The workflow creation and webhook flow was tested against n8n 2.35.7 with a scripted stand-in for
  Ollama; it has not been run against a real downloaded model yet, so do one real test chat first.

## Useful commands

```bash
docker compose logs -f dashboard      # agent / tool errors
docker compose ps                     # what is running
docker compose down                   # stop (data volumes are kept)
docker compose up -d --build          # rebuild after code changes
docker compose exec ollama ollama pull llama3.1:8b   # download another model
```

## Files

- `app.py` agent loop, tools, n8n workflow templates, client CRM
- `templates/index.html` chat UI (mobile friendly)
- `docker-compose.yml`, `Dockerfile.dashboard`, `nginx/default.conf` the stack
- `.env.example` all settings
