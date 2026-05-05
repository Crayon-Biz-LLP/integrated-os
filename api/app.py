# api/app.py - Entry point for Vercel Python runtime
from api.index import app

# This file exposes 'app' at the top level for Vercel
if __name__ != "__main__":
    pass  # Vercel uses the 'app' instance
