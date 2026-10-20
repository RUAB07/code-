# AI Workflow Stack with Docker

This repository contains a simple Docker-based stack for running:
- n8n for automation/workflows
- Ollama for local LLM models
- PostgreSQL for n8n data
- a dev container for GitHub Codespaces

## Included files
- `Dockerfile`
- `docker-compose.yml`
- `.env.example`
- `.devcontainer/devcontainer.json`

## Quick start

1. Copy the environment file:
   ```bash
   cp .env.example .env
   ```

2. Start the services:
   ```bash
   docker compose up -d
   ```

3. Open the apps:
   - n8n: http://localhost:5678
   - Ollama: http://localhost:11434
   - PostgreSQL: localhost:5432

## GitHub Codespaces

This repo includes a Codespaces dev container configuration. Open the repository in GitHub Codespaces and the dev environment will be ready automatically.

## Useful commands

```bash
# View logs

docker compose logs -f

# Stop everything

docker compose down

# Rebuild after changes

docker compose up -d --build
```

## Example LLM models

After Ollama is running, you can pull models such as:

```bash
curl -X POST http://localhost:11434/api/pull -d '{"name":"llama3.1"}'
```

or:

```bash
curl -X POST http://localhost:11434/api/pull -d '{"name":"mistral"}'
```

## Notes

- Change the `.env` values before using in a real setup.
- For production, use a strong password and secure environment variables.
- If you want to use a public cloud server instead of Codespaces, run the same Docker Compose on a VPS or VM.
