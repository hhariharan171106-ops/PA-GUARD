import os
from pathlib import Path
from typing import List

# Core runtime configuration
API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = int(os.getenv("API_PORT", "8000"))

# Repository root (directory containing this file's parent)
BASE_DIR = Path(__file__).resolve().parent.parent

# Database path – default to repository-relative location; can be overridden via env var PA_GUARD_DB
DB_PATH: Path = Path(os.getenv("PA_GUARD_DB", Path(__file__).resolve().parent / "pa_guard.db"))

# Document storage directory (relative to backend folder)
DOCUMENT_STORAGE_DIR: Path = Path(__file__).resolve().parent / "document_storage"

# OCR / PDF utilities
TESSERACT_PATH: str | None = os.getenv("TESSERACT_PATH")
POPPLER_PATH: str | None = os.getenv("POPPLER_PATH")

# LLM / RAG configuration
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3.5:latest")
OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_GENERATION_TIMEOUT_SECONDS: int = max(
    5, min(60, int(os.getenv("OLLAMA_GENERATION_TIMEOUT_SECONDS", "30")))
)
OLLAMA_KEEP_ALIVE: str = os.getenv("OLLAMA_KEEP_ALIVE", "2m")
OLLAMA_GENERATION_OPTIONS = {
    "temperature": 0,
    "num_predict": 128,
    "num_ctx": 2048,
}

# CORS / Front‑end integration – comma‑separated list, default to allow all (development)
FRONTEND_ORIGINS: List[str] = [origin.strip() for origin in os.getenv("FRONTEND_ORIGINS", "*").split(",")]

# Logging configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
