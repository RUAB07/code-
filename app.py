from flask import Flask, request, jsonify
import os
import requests

app = Flask(__name__)

# Environment-driven model/backend toggle.
OLLAMA_MODE = os.getenv("OLLAMA_MODE", "cloud")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "https://ollama.com/api")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.1")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/ai-chat")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "mode": OLLAMA_MODE,
        "ollama_base_url": OLLAMA_BASE_URL,
        "n8n": N8N_WEBHOOK_URL,
    })

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        return jsonify({"error": "Prompt is required."}), 400

    try:
        # Use cloud Ollama API securely from server-side only.
        if OLLAMA_MODE == "cloud":
            if not OLLAMA_API_KEY:
                return jsonify({"error": "OLLAMA_API_KEY is missing."}), 500

            response = requests.post(
                f"{OLLAMA_BASE_URL}/chat",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OLLAMA_API_KEY}",
                },
                json={
                    "model": DEFAULT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                timeout=120,
            )

            if response.status_code >= 400:
                return jsonify({"error": f"Ollama cloud API error: {response.text}"}), 502

            result = response.json()
            reply = result.get("message", {}).get("content", "").strip()

            if not reply:
                return jsonify({"error": "Model returned an empty response."}), 502

            return jsonify({"reply": reply})

        # Local Ollama mode for Docker/Codespaces/VM setups.
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": DEFAULT_MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=120,
        )

        if response.status_code >= 400:
            return jsonify({"error": f"Ollama local API error: {response.text}"}), 502

        result = response.json()
        reply = result.get("response", "").strip()

        if not reply:
            return jsonify({"error": "Model returned an empty response."}), 502

        return jsonify({"reply": reply})

    except Exception as exc:
        return jsonify({"error": f"Failed to process request: {str(exc)}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=False)
