"""Loading the CV, and stripping its private data before it leaves the machine."""
import re
from pathlib import Path

# The master CV holds contact rows and internal notes; only job-relevant facts may leave.
CONTACT_ROW = re.compile(r"^\|\s*(Full name|Email|Phone|LinkedIn|GitHub|Personal site.*)\s*\|.*$", re.MULTILINE)
INTERNAL_LINE = re.compile(r"^.*\[INTERNAL\].*$\n?", re.MULTILINE)
CV_RULE_LINE = re.compile(r"^\s*`?\[CV-RULE\]`?.*$\n?", re.MULTILINE)
META_TAG = re.compile(r"`?\[(CV-RULE|TODO[^\]]*|GUESSED|UNCONFIRMED)\]`?")


def load_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text or text.startswith("<!--"):
        raise ValueError(f"{path} looks empty — fill it in before running.")
    return text


def sanitize_cv(text: str) -> str:
    text = text.split("---", 1)[-1]  # drop header blockquote (tags legend, "never sent" notice)
    text = INTERNAL_LINE.sub("", text)
    text = CV_RULE_LINE.sub("", text)
    text = CONTACT_ROW.sub("", text)
    text = META_TAG.sub("", text)
    return text.strip()
