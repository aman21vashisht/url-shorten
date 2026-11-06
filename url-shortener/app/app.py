
import os
import string
import secrets

import psycopg2
import redis

from flask import Flask, request, redirect, jsonify, render_template_string
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# Configuration from Docker environment variables
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@db:5432/urlshortener"
)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Redis connection
cache = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=3
)

# Homepage
HOME_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>URL Shortener</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 650px;
            margin: 80px auto;
            padding: 20px;
            background: #f4f7fb;
            color: #222;
        }
        .container {
            background: white;
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 4px 18px #0001;
        }
        input {
            box-sizing: border-box;
            width: 100%;
            padding: 12px;
            margin: 12px 0;
        }
        button {
            background: #1769e0;
            color: white;
            padding: 12px 20px;
            border: 0;
            border-radius: 6px;
            cursor: pointer;
        }
        #result {
            margin-top: 20px;
            overflow-wrap: anywhere;
        }
    </style>
</head>
<body>
<div class="container">
    <h1>URL Shortener</h1>
    <p>Convert a long URL into a short link.</p>
    <form id="shorten-form">
        <input type="url" id="url" placeholder="https://example.com"
               required>
        <button type="submit">Shorten URL</button>
    </form>
    <div id="result"></div>
</div>
<script>
document.getElementById("shorten-form").addEventListener("submit",
    async function(event) {
        event.preventDefault();
        const result = document.getElementById("result");
        result.textContent = "Creating short link...";

        try {
            const response = await fetch("/shorten", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    url: document.getElementById("url").value
                })
            });
            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || "Request failed");
            }

            result.replaceChildren();
            const message = document.createElement("p");
            message.textContent = "Your short URL:";
            const link = document.createElement("a");
            link.href = data.short_url;
            link.textContent = data.short_url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            result.append(message, link);
        } catch (error) {
            result.textContent = "Error: " + error.message;
        }
    }
);
</script>
</body>
</html>
"""


def get_db_connection():
    """Create a PostgreSQL connection."""
    return psycopg2.connect(DATABASE_URL, connect_timeout=5)


def generate_short_code(length=7):
    """Generate a random URL-safe short code."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@app.route("/", methods=["GET"])
def home():
    """Display the homepage instead of returning 404."""
    return render_template_string(HOME_PAGE)


@app.route("/health", methods=["GET"])
def health():
    """Check whether PostgreSQL and Redis are reachable."""
    status = {"app": "ok", "postgres": "unknown", "redis": "unknown"}
    http_status = 200

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        status["postgres"] = "ok"
    except Exception:
        status["postgres"] = "error"
        http_status = 503

    try:
        cache.ping()
        status["redis"] = "ok"
    except Exception:
        status["redis"] = "error"
        http_status = 503

    return jsonify(status), http_status


@app.route("/shorten", methods=["POST"])
def shorten_url():
    """Save a long URL and return its short URL."""
    data = request.get_json(silent=True) or {}
    original_url = data.get("url", "").strip()

    if not original_url:
        return jsonify({"error": "Please provide a URL."}), 400

    if not original_url.startswith(("http://", "https://")):
        return jsonify({"error": "URL must start with http:// or https://."}), 400

    # Retry if a randomly generated code already exists.
    for _ in range(5):
        short_code = generate_short_code()

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO urls (short_code, original_url)
                        VALUES (%s, %s)
                        ON CONFLICT (short_code) DO NOTHING
                        RETURNING short_code
                        """,
                        (short_code, original_url),
                    )
                    inserted = cur.fetchone()

            if inserted:
                try:
                    cache.set(short_code, original_url, ex=3600)
                except redis.RedisError:
                    app.logger.warning("Redis unavailable; URL saved in PostgreSQL.")

                short_url = request.host_url.rstrip("/") + "/" + short_code
                return jsonify({
                    "short_code": short_code,
                    "original_url": original_url,
                    "short_url": short_url
                }), 201

        except psycopg2.Error:
            app.logger.exception("Could not save URL in PostgreSQL.")
            return jsonify({"error": "Database error. Check server logs."}), 500

    return jsonify({"error": "Could not generate a unique short code."}), 500


@app.route("/<string:short_code>", methods=["GET"])
def redirect_url(short_code):
    """Resolve a short code using Redis first, then PostgreSQL."""
    if short_code in {"favicon.ico", "health"}:
        if short_code == "favicon.ico":
            return "", 204

    # Fast path: Redis cache
    try:
        original_url = cache.get(short_code)
        if original_url:
            app.logger.info("CACHE HIT - %s", short_code)
            return redirect(original_url, code=302)
    except redis.RedisError:
        app.logger.warning("Redis unavailable; checking PostgreSQL.")

    app.logger.info("CACHE MISS - querying database")

    # Fallback: PostgreSQL
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT original_url FROM urls WHERE short_code = %s",
                    (short_code,),
                )
                row = cur.fetchone()

        if not row:
            return jsonify({"error": "Short URL not found."}), 404

        original_url = row[0]

        try:
            cache.set(short_code, original_url, ex=3600)
        except redis.RedisError:
            app.logger.warning("Could not cache URL in Redis.")

        return redirect(original_url, code=302)

    except psycopg2.Error:
        app.logger.exception("Could not retrieve URL from PostgreSQL.")
        return jsonify({"error": "Database error. Check server logs."}), 500


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Route not found."}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
