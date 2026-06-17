"""
Minimal setup.py for Agent Engine deployment.

The Vertex AI SDK's extra_packages parameter uses pip to install local
directories — it needs a setup.py (or pyproject.toml) to build a wheel.

This is NOT used for local development or Docker — only for Agent Engine.
"""
from setuptools import setup, find_packages

setup(
    name        = "intellidraft-data-ingestion",
    version     = "1.0.0",
    packages    = find_packages(exclude=["local_storage*", "*.db", "tests*"]),
    install_requires = [],   # requirements handled separately by ReasoningEngine.create()
    python_requires  = ">=3.11",
)
