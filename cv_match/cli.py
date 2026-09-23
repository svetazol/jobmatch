"""Entry point: load a CV and a job description, run the match, print the result."""
import os

from dotenv import load_dotenv

from .config import API_KEY_ENV, CV_PATH, JOB_PATH
from .matcher import match
from .sanitize import load_text, sanitize_cv


def main() -> None:
    load_dotenv()
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        raise SystemExit(f"Set {API_KEY_ENV} in your environment.")

    cv = sanitize_cv(load_text(CV_PATH))
    job = load_text(JOB_PATH)

    result = match(api_key, cv, job)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
