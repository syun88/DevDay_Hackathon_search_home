"""Vercel Function for every /api/* route: the same WSGI bridge as vercel_app.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vercel_app import app  # noqa: E402
