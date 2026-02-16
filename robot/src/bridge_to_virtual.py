# -*- coding: utf-8 -*-
"""Backward-compatible entry point for the unified Pepper bridge server."""

from __future__ import print_function, unicode_literals

from bridge import app  # noqa: F401


if __name__ == "__main__":
    print("[INFO] bridge_to_virtual has been merged into bridge.py. Starting unified server...")
    app.run(host="0.0.0.0", port=5000)
