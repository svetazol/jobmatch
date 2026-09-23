"""Paths and settings for the CV/job matcher."""
from pathlib import Path

API_KEY_ENV = "OPENROUTER_API_KEY"
BASE_URL = "https://openrouter.ai/api"
MODEL = "jev-latest"

CV_PATH = Path("data/cv.md")
JOB_PATH = Path("data/job.md")
