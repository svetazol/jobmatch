"""Talking to Jev (TypeSafe AI's System One model) via OpenRouter."""
from typesafe_sdk import TypeSafeClient

from .config import BASE_URL, MODEL
from .questions import QUESTIONS


def match(api_key: str, cv: str, job: str):
    with TypeSafeClient(api_key=api_key, base_url=BASE_URL) as client:
        return client.system_one(
            state={"cv": cv, "job_description": job},
            questions=QUESTIONS,
            model=MODEL,
        )
