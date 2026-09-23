"""Loading input files and stripping private CV data before it leaves the machine."""
import re
from pathlib import Path

# data/cv.md is a private master database ("never sent to anyone" — see its own
# header) with contact info and internal-only notes. Strip that before it leaves
# the machine; only job-relevant facts go to Jev.
CONTACT_ROW = re.compile(r"^\|\s*(Full name|Email|Phone|LinkedIn|GitHub|Personal site.*)\s*\|.*$", re.MULTILINE)
INTERNAL_LINE = re.compile(r"^.*\[INTERNAL\].*$\n?", re.MULTILINE)
CV_RULE_LINE = re.compile(r"^\s*`?\[CV-RULE\]`?.*$\n?", re.MULTILINE)
META_TAG = re.compile(r"`?\[(CV-RULE|TODO[^\]]*|GUESSED|UNCONFIRMED)\]`?")


def load_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text or text.startswith("<!--"):
        raise SystemExit(f"{path} looks empty — fill it in before running.")
    return text


def sanitize_cv(text: str) -> str:
    text = text.split("---", 1)[-1]  # drop header blockquote (tags legend, "never sent" notice)
    text = INTERNAL_LINE.sub("", text)
    text = CV_RULE_LINE.sub("", text)
    text = CONTACT_ROW.sub("", text)
    text = META_TAG.sub("", text)
    return text.strip()
