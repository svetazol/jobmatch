#!/usr/bin/env python3
"""Match data/cv.md against data/job.md using Jev (docs/jev.md), via OpenRouter."""
import json
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = "https://openrouter.ai/api/v1/systemone"
MODEL = "jev-latest"

CV_PATH = Path("data/cv.md")
JOB_PATH = Path("data/job.md")

# data/cv.md is a private master database ("never sent to anyone" — see its own
# header) with contact info and internal-only notes. Strip that before it leaves
# the machine; only job-relevant facts go to Jev.
CONTACT_ROW = re.compile(r"^\|\s*(Full name|Email|Phone|LinkedIn|GitHub|Personal site.*)\s*\|.*$", re.MULTILINE)
INTERNAL_LINE = re.compile(r"^.*\[INTERNAL\].*$\n?", re.MULTILINE)
CV_RULE_LINE = re.compile(r"^\s*`?\[CV-RULE\]`?.*$\n?", re.MULTILINE)
META_TAG = re.compile(r"`?\[(CV-RULE|TODO[^\]]*|GUESSED|UNCONFIRMED)\]`?")


def sanitize_cv(text: str) -> str:
    text = text.split("---", 1)[-1]  # drop header blockquote (tags legend, "never sent" notice)
    text = INTERNAL_LINE.sub("", text)
    text = CV_RULE_LINE.sub("", text)
    text = CONTACT_ROW.sub("", text)
    text = META_TAG.sub("", text)
    return text.strip()

QUESTIONS = {
    "is_qualified": {
        "type": "noul",
        "instructions": "Does the candidate meet the job's minimum required qualifications?",
        "criteria": {
            "true": "Candidate meets all must-have requirements.",
            "false": "Candidate is missing one or more must-have requirements.",
        },
    },
    "overall_fit": {
        "type": "score",
        "instructions": "How strong is the overall fit between the candidate's CV and the job description?",
        "criteria": [
            {"label": "poor", "description": "Major mismatch in skills/experience."},
            {"label": "weak", "description": "Some relevant experience but significant gaps."},
            {"label": "good", "description": "Solid match with minor gaps."},
            {"label": "strong", "description": "Very close match to requirements."},
            {"label": "excellent", "description": "Ideal match, exceeds requirements."},
        ],
    },
    "top_gap": {
        "type": "choice",
        "instructions": "What is the single biggest gap, if any, between the candidate and this job?",
        "criteria": {
            "technical_skills": "Missing or weak technical/tooling skills.",
            "domain_experience": "Missing relevant industry or domain experience.",
            "seniority_level": "Under- or over-qualified for the seniority level.",
            "none": "No significant gap.",
        },
    },
}


def load(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text or text.startswith("<!--"):
        raise SystemExit(f"{path} looks empty — fill it in before running.")
    return text


def main() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENROUTER_API_KEY in your environment.")

    cv = sanitize_cv(load(CV_PATH))
    job = load(JOB_PATH)

    resp = requests.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "state": {"cv": cv, "job_description": job},
            "questions": QUESTIONS,
        },
        timeout=30,
    )
    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


if __name__ == "__main__":
    main()
