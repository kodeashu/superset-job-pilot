"""
Production WSGI entrypoint for Cloud Deployment (Render, Railway, Fly.io, Koyeb, Docker).
"""
import os
from web_server import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
