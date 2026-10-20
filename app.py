from flask import Flask, render_template, request, jsonify
import os
import requests

app = Flask(__name__)

N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/ai-chat")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.1")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "n8n": N8N_WEBHOOK_URL, "ollama": OLLAMA_URL})

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        return jsonify({"error": "Prompt is required."}), 400

    try:
        payload = {
            "prompt": prompt,
            "model": DEFAULT_MODEL,
            "stream": False,
        }

        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=120,
        )

        if response.status_code >= 400:
            return jsonify({"error": f"Ollama API error: {response.text}"}), 502

        result = response.json()
        reply = result.get("response", "").strip()
        if not reply:
            return jsonify({"error": "Model returned an empty response."}), 502

        return jsonify({"reply": reply})
    except Exception as exc:
        return jsonify({"error": f"Failed to process request: {str(exc)}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=False)
