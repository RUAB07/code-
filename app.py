"""AI Agency Agent: a chat app where an Ollama-powered agent runs a small
automation agency by calling n8n.

Flow:
  browser chat -> Flask (/api/chat) -> Ollama /api/chat (tool calling)
                                   -> tools -> n8n public API / webhooks + SQLite CRM
"""

import hmac
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import requests
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

# --------------------------------------------------------------------------
# Configuration (all overridable with environment variables)
# --------------------------------------------------------------------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.2:3b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))

N8N_URL = os.getenv("N8N_URL", "http://localhost:5678").rstrip("/")
N8N_API_KEY = os.getenv("N8N_API_KEY", "").strip()
# URL that a client's website / form would call (what the outside world sees).
PUBLIC_WEBHOOK_BASE = os.getenv("WEBHOOK_URL", "http://localhost:5678/").rstrip("/")
# How the n8n container reaches Ollama (inside the Docker network).
OLLAMA_URL_FOR_N8N = os.getenv("OLLAMA_URL_FOR_N8N", "http://ollama:11434").rstrip("/")
AUTOMATION_MODEL = os.getenv("AUTOMATION_MODEL", DEFAULT_MODEL)

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
DATA_DIR = os.getenv("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "agency.db")

MAX_AGENT_STEPS = 6
MAX_HISTORY_MESSAGES = 40


# --------------------------------------------------------------------------
# Optional password protection (HTTP basic auth, password only)
# --------------------------------------------------------------------------
@app.before_request
def require_password():
    if not DASHBOARD_PASSWORD or request.path == "/health":
        return None
    auth = request.authorization
    supplied = (auth.password or "") if auth else ""
    if hmac.compare_digest(supplied.encode(), DASHBOARD_PASSWORD.encode()):
        return None
    return Response(
        "Login required.",
        401,
        {"WWW-Authenticate": 'Basic realm="AI Agency Agent"'},
    )


# --------------------------------------------------------------------------
# Tiny SQLite CRM: clients + the automations deployed for them
# --------------------------------------------------------------------------
@contextmanager
def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                business_type TEXT NOT NULL,
                contact TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS automations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id),
                kind TEXT NOT NULL,
                n8n_workflow_id TEXT NOT NULL,
                webhook_path TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Automation templates: each becomes  Webhook -> Ollama -> Respond to Webhook
# --------------------------------------------------------------------------
TEMPLATES = {
    "faq_chatbot": {
        "title": "FAQ and booking chatbot",
        "description": "Answers customer questions and collects booking details. "
        "Embed it in the client's website or Facebook page.",
        "system": (
            "You are the friendly virtual assistant of {name}, a {business_type}. {details} "
            "Answer customer questions briefly and politely, in the customer's language "
            "(Bangla or English). If you do not know something, say the team will follow up "
            "and ask for their phone number. For booking requests collect: name, phone, "
            "preferred date and time. Never invent prices or opening hours that are not "
            "given above."
        ),
    },
    "review_reply": {
        "title": "Google review auto-reply",
        "description": "Drafts a warm reply to a customer review (send the review text as the message).",
        "system": (
            "You write replies to customer reviews for {name}, a {business_type}. {details} "
            "The user message is the customer's review. Reply in the same language as the "
            "review, in 2-4 sentences, warm and professional. Thank positive reviewers. For "
            "negative reviews apologise, do not argue, and invite them to contact the "
            "business directly. Output only the reply text."
        ),
    },
    "lead_followup": {
        "title": "Lead follow-up message",
        "description": "Writes a short WhatsApp/SMS-style follow-up for a new lead "
        "(send the lead's name and what they asked about).",
        "system": (
            "You write short follow-up messages (WhatsApp or SMS style, under 60 words) for "
            "{name}, a {business_type}. {details} The user message describes a lead and what "
            "they asked about. Write a friendly message that answers their interest and asks "
            "one clear next-step question. Output only the message."
        ),
    },
    "social_post": {
        "title": "Social media caption writer",
        "description": "Writes a Facebook/Instagram caption for an offer or topic "
        "(send the topic as the message). Good monthly content upsell.",
        "system": (
            "You write social media captions for {name}, a {business_type}. {details} The "
            "user message is the topic or offer. Write one Facebook/Instagram caption with "
            "3-5 relevant hashtags, in the language of the request unless told otherwise. "
            "Output only the caption."
        ),
    },
}


def build_workflow(kind, client, details, webhook_path):
    """Return an n8n workflow (public API format) for a template."""
    system_prompt = TEMPLATES[kind]["system"].format(
        name=client["name"],
        business_type=client["business_type"],
        details=(details or client["notes"] or "").strip(),
    ).replace("  ", " ")

    # The client's text goes into a Set node as a plain (non-expression) value. Putting it
    # inside an n8n expression would break as soon as it contains "{{" or "}}".
    body_expression = (
        "={{ JSON.stringify({ model: "
        + json.dumps(AUTOMATION_MODEL)
        + ", stream: false, messages: [ { role: 'system', content: $json.system_prompt }, "
        "{ role: 'user', content: $json.message } ] }) }}"
    )
    fallback = "Sorry, I could not answer right now. Please try again in a moment."
    response_expression = (
        "={{ { reply: ($json.message && $json.message.content) || "
        + json.dumps(fallback)
        + ", ok: !!($json.message && $json.message.content) } }}"
    )

    return {
        "name": f"{client['name']} - {TEMPLATES[kind]['title']}",
        "nodes": [
            {
                "id": str(uuid.uuid4()),
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [0, 0],
                "webhookId": str(uuid.uuid4()),
                "parameters": {
                    "httpMethod": "POST",
                    "path": webhook_path,
                    "responseMode": "responseNode",
                    "options": {},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Prepare prompt",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [240, 0],
                "parameters": {
                    "assignments": {
                        "assignments": [
                            {
                                "id": str(uuid.uuid4()),
                                "name": "system_prompt",
                                "value": system_prompt,
                                "type": "string",
                            },
                            {
                                "id": str(uuid.uuid4()),
                                "name": "message",
                                "value": "={{ String($json.body.message || '') }}",
                                "type": "string",
                            },
                        ]
                    },
                    "includeOtherFields": False,
                    "options": {},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Ask Ollama",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [480, 0],
                "onError": "continueRegularOutput",
                "parameters": {
                    "method": "POST",
                    "url": f"{OLLAMA_URL_FOR_N8N}/api/chat",
                    "sendBody": True,
                    "specifyBody": "json",
                    "jsonBody": body_expression,
                    "options": {"timeout": 300000},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Respond",
                "type": "n8n-nodes-base.respondToWebhook",
                "typeVersion": 1.1,
                "position": [720, 0],
                "parameters": {
                    "respondWith": "json",
                    "responseBody": response_expression,
                    "options": {},
                },
            },
        ],
        "connections": {
            "Webhook": {"main": [[{"node": "Prepare prompt", "type": "main", "index": 0}]]},
            "Prepare prompt": {"main": [[{"node": "Ask Ollama", "type": "main", "index": 0}]]},
            "Ask Ollama": {"main": [[{"node": "Respond", "type": "main", "index": 0}]]},
        },
        "settings": {"executionOrder": "v1"},
    }


# --------------------------------------------------------------------------
# n8n helpers
# --------------------------------------------------------------------------
class ToolError(Exception):
    """A problem the agent can read and react to."""


def n8n_api(method, path, **kwargs):
    if not N8N_API_KEY:
        raise ToolError(
            "N8N_API_KEY is not set. In n8n open Settings > n8n API, create an API key, "
            "put it in the .env file as N8N_API_KEY, then restart the dashboard."
        )
    try:
        response = requests.request(
            method,
            f"{N8N_URL}/api/v1{path}",
            headers={"X-N8N-API-KEY": N8N_API_KEY, "Accept": "application/json"},
            timeout=30,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise ToolError(f"Could not reach n8n at {N8N_URL}: {exc}")
    if response.status_code >= 400:
        raise ToolError(f"n8n API error {response.status_code}: {response.text[:300]}")
    return response.json() if response.text else {}


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "client"


# --------------------------------------------------------------------------
# Agent tools
# --------------------------------------------------------------------------
def tool_add_client(name, business_type, contact="", notes=""):
    name, business_type = str(name).strip(), str(business_type).strip()
    if not name or not business_type:
        raise ToolError("Both name and business_type are required.")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO clients (name, business_type, contact, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, business_type, str(contact or ""), str(notes or ""), now_iso()),
        )
        return {"client_id": cur.lastrowid, "name": name, "business_type": business_type}


def tool_list_clients():
    with db() as conn:
        rows = conn.execute(
            "SELECT c.id, c.name, c.business_type, c.contact, c.notes, "
            "(SELECT COUNT(*) FROM automations a WHERE a.client_id = c.id) AS automations "
            "FROM clients c ORDER BY c.id"
        ).fetchall()
    return {"clients": [dict(r) for r in rows]}


def tool_list_automation_types():
    return {
        "automation_types": [
            {"kind": kind, "title": t["title"], "description": t["description"]}
            for kind, t in TEMPLATES.items()
        ]
    }


def tool_deploy_automation(client_id, kind, details=""):
    if kind not in TEMPLATES:
        raise ToolError(f"Unknown kind '{kind}'. Valid kinds: {', '.join(TEMPLATES)}.")
    with db() as conn:
        client = conn.execute("SELECT * FROM clients WHERE id = ?", (int(client_id),)).fetchone()
    if not client:
        raise ToolError(f"No client with id {client_id}. Use list_clients or add_client first.")

    path = f"{kind}-{slugify(client['name'])}-{uuid.uuid4().hex[:6]}"
    workflow = build_workflow(kind, dict(client), str(details or ""), path)
    created = n8n_api("POST", "/workflows", json=workflow)
    workflow_id = str(created["id"])
    n8n_api("POST", f"/workflows/{workflow_id}/activate")

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO automations (client_id, kind, n8n_workflow_id, webhook_path, active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (client["id"], kind, workflow_id, path, now_iso()),
        )
        automation_id = cur.lastrowid
    return {
        "automation_id": automation_id,
        "client": client["name"],
        "kind": kind,
        "n8n_workflow_id": workflow_id,
        "active": True,
        "webhook_url": f"{PUBLIC_WEBHOOK_BASE}/webhook/{path}",
        "how_to_use": 'POST JSON {"message": "..."} to the webhook_url; the reply comes back as {"reply": "..."}.',
    }


def tool_list_automations(client_id=None):
    query = (
        "SELECT a.id, a.client_id, c.name AS client, a.kind, a.n8n_workflow_id, "
        "a.webhook_path, a.active FROM automations a JOIN clients c ON c.id = a.client_id"
    )
    params = ()
    if client_id not in (None, ""):
        query += " WHERE a.client_id = ?"
        params = (int(client_id),)
    with db() as conn:
        rows = conn.execute(query + " ORDER BY a.id", params).fetchall()
    return {
        "automations": [
            {**dict(r), "active": bool(r["active"]), "webhook_url": f"{PUBLIC_WEBHOOK_BASE}/webhook/{r['webhook_path']}"}
            for r in rows
        ]
    }


def _get_automation(automation_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM automations WHERE id = ?", (int(automation_id),)).fetchone()
    if not row:
        raise ToolError(f"No automation with id {automation_id}. Use list_automations.")
    return row


def tool_test_automation(automation_id, message):
    row = _get_automation(automation_id)
    if not row["active"]:
        raise ToolError("That automation is switched off. Use set_automation_active first.")
    try:
        response = requests.post(
            f"{N8N_URL}/webhook/{row['webhook_path']}",
            json={"message": str(message)},
            timeout=OLLAMA_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ToolError(f"Could not reach the n8n webhook: {exc}")
    if response.status_code >= 400:
        raise ToolError(f"Webhook returned {response.status_code}: {response.text[:300]}")
    try:
        body = response.json()
    except ValueError:
        raise ToolError(f"The webhook did not return JSON (got: {response.text[:200]!r}). Check the workflow in n8n.")
    if not body.get("ok", False):
        raise ToolError(
            "The workflow ran but the model gave no answer. Check that Ollama is running and the model is downloaded."
        )
    return {"reply": body.get("reply", "")}


def tool_set_automation_active(automation_id, active):
    row = _get_automation(automation_id)
    if isinstance(active, str):
        active = active.strip().lower() in ("true", "1", "yes", "on")
    n8n_api("POST", f"/workflows/{row['n8n_workflow_id']}/{'activate' if active else 'deactivate'}")
    with db() as conn:
        conn.execute("UPDATE automations SET active = ? WHERE id = ?", (1 if active else 0, row["id"]))
    return {"automation_id": row["id"], "active": bool(active)}


def tool_n8n_status():
    try:
        health = requests.get(f"{N8N_URL}/healthz", timeout=5).status_code == 200
    except requests.RequestException:
        health = False
    key_valid = None
    if health and N8N_API_KEY:
        try:
            key_valid = requests.get(
                f"{N8N_URL}/api/v1/workflows?limit=1",
                headers={"X-N8N-API-KEY": N8N_API_KEY},
                timeout=5,
            ).status_code == 200
        except requests.RequestException:
            key_valid = False
    return {"n8n_reachable": health, "api_key_set": bool(N8N_API_KEY), "api_key_valid": key_valid}


TOOL_FUNCS = {
    "add_client": tool_add_client,
    "list_clients": tool_list_clients,
    "list_automation_types": tool_list_automation_types,
    "deploy_automation": tool_deploy_automation,
    "list_automations": tool_list_automations,
    "test_automation": tool_test_automation,
    "set_automation_active": tool_set_automation_active,
    "n8n_status": tool_n8n_status,
}


def _fn(name, description, properties=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


TOOL_SCHEMAS = [
    _fn(
        "add_client",
        "Save a new client business in the agency CRM. Returns client_id.",
        {
            "name": {"type": "string", "description": "Business name"},
            "business_type": {"type": "string", "description": "e.g. restaurant, salon, gym, clinic"},
            "contact": {"type": "string", "description": "Phone, email or owner name"},
            "notes": {"type": "string", "description": "Hours, services, prices, tone - facts the bot may use"},
        },
        ["name", "business_type"],
    ),
    _fn("list_clients", "List all clients in the CRM with their ids."),
    _fn("list_automation_types", "List the automation kinds that can be deployed to n8n."),
    _fn(
        "deploy_automation",
        "Build an n8n workflow for a client, activate it, and return its webhook URL.",
        {
            "client_id": {"type": "integer"},
            "kind": {"type": "string", "enum": list(TEMPLATES)},
            "details": {
                "type": "string",
                "description": "Extra facts for this automation (hours, menu, tone). Optional.",
            },
        },
        ["client_id", "kind"],
    ),
    _fn(
        "list_automations",
        "List deployed automations and their webhook URLs, optionally for one client.",
        {"client_id": {"type": "integer"}},
    ),
    _fn(
        "test_automation",
        "Send a test message through a deployed automation's n8n webhook and return the reply.",
        {"automation_id": {"type": "integer"}, "message": {"type": "string"}},
        ["automation_id", "message"],
    ),
    _fn(
        "set_automation_active",
        "Switch a deployed automation on or off in n8n.",
        {"automation_id": {"type": "integer"}, "active": {"type": "boolean"}},
        ["automation_id", "active"],
    ),
    _fn("n8n_status", "Check whether n8n is reachable and the API key works."),
]

SYSTEM_PROMPT = (
    "You are an AI agent that helps a solo freelancer run an automation agency for local "
    "businesses (restaurants, salons, gyms, clinics). You work through tools: a client CRM and "
    "n8n workflows.\n"
    "Rules:\n"
    "- To do anything (save a client, deploy, test, switch on/off) you MUST call the matching "
    "tool. Never say you did something unless a tool result confirms it.\n"
    "- If required details are missing (client name, business type, which automation), ask a "
    "short question first instead of guessing.\n"
    "- Look up ids with list_clients / list_automations instead of making them up.\n"
    "- If a tool returns an error, explain it in plain words and say what the user should do.\n"
    "- Reply in the language the user writes in (Bangla, English or a mix). Keep replies short "
    "and practical. When an automation is deployed, show its webhook URL."
)


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------
def extract_text_tool_call(content):
    """Small models sometimes print a tool call as JSON text. Accept that too."""
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if isinstance(obj, dict) and obj.get("name") in TOOL_FUNCS:
        args = obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, dict):
            return {"function": {"name": obj["name"], "arguments": args}}
    return None


def run_tool(name, arguments):
    func = TOOL_FUNCS.get(name)
    if not func:
        return {"error": f"Unknown tool '{name}'. Available: {', '.join(TOOL_FUNCS)}."}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {"error": "Tool arguments were not valid JSON."}
    try:
        return func(**(arguments or {}))
    except ToolError as exc:
        return {"error": str(exc)}
    except TypeError as exc:
        return {"error": f"Bad arguments for {name}: {exc}"}
    except (ValueError, KeyError) as exc:
        return {"error": f"Invalid input for {name}: {exc}"}


def trim_history(messages):
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    start = len(messages) - MAX_HISTORY_MESSAGES
    while start < len(messages) and messages[start].get("role") != "user":
        start += 1
    return messages[start:]


def call_ollama(messages):
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                "tools": TOOL_SCHEMAS,
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=OLLAMA_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ToolError(f"Could not reach Ollama at {OLLAMA_URL}: {exc}")
    if response.status_code == 404:
        raise ToolError(
            f"Model '{DEFAULT_MODEL}' is not downloaded yet. Run: docker compose exec ollama ollama pull {DEFAULT_MODEL}"
        )
    if response.status_code >= 400:
        raise ToolError(f"Ollama error {response.status_code}: {response.text[:300]}")
    return response.json().get("message", {})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Prompt is required."}), 400

    history = [
        m for m in (data.get("messages") or [])
        if isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
    ]
    messages = trim_history(history) + [{"role": "user", "content": prompt}]
    trace = []
    reply = ""

    try:
        for _ in range(MAX_AGENT_STEPS):
            message = call_ollama(messages)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                fallback = extract_text_tool_call(message.get("content"))
                tool_calls = [fallback] if fallback else []

            if not tool_calls:
                reply = (message.get("content") or "").strip()
                messages.append({"role": "assistant", "content": reply})
                break

            messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
            for call in tool_calls:
                fn = call.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments", {})
                result = run_tool(name, args)
                trace.append({"tool": name, "args": args, "result": result})
                messages.append(
                    {"role": "tool", "tool_name": name, "content": json.dumps(result)[:4000]}
                )
        else:
            reply = "I used several tools but could not finish. Check the steps above and ask me to continue."
            messages.append({"role": "assistant", "content": reply})
    except ToolError as exc:
        return jsonify({"error": str(exc)}), 502

    if not reply:
        reply = "Done." if trace else "The model returned an empty answer. Please try again."
    return jsonify({"reply": reply, "trace": trace, "messages": messages})


# --------------------------------------------------------------------------
# Pages and small JSON endpoints for the side panels
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", model=DEFAULT_MODEL)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/status")
def status():
    ollama = {"reachable": False, "model": DEFAULT_MODEL, "model_ready": False}
    try:
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()
        names = [m.get("name", "") for m in tags.get("models", [])]
        ollama["reachable"] = True
        ollama["model_ready"] = any(n == DEFAULT_MODEL or n.split(":")[0] == DEFAULT_MODEL for n in names)
    except (requests.RequestException, ValueError):
        pass
    return jsonify({"ollama": ollama, "n8n": tool_n8n_status()})


@app.route("/api/clients")
def clients():
    return jsonify({**tool_list_clients(), **tool_list_automations()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=False, threaded=True)
