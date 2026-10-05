"""The prompt `claude -p` gets. No subprocess runs here: the endpoint's tests
in test_api.py stub `cover_note.write`."""
from jobmatch import cover_note
from jobmatch.matching.questions import BEST_ANGLE, TOP_GAP
from jobmatch.models import MatchResult, Vacancy


def _vacancy(title, description):
    return Vacancy(title=title, description=description, company="Co", skills=["Python"])


def test_russian_posting_with_english_terms_is_russian():
    v = _vacancy("Python-разработчик", "Ищем backend разработчика: Django, PostgreSQL, опыт от 3 лет.")
    assert cover_note.language_of(v) == "Russian"


def test_english_posting_is_english():
    v = _vacancy("Senior Python Developer", "We need a backend engineer with Django experience.")
    assert cover_note.language_of(v) == "English"


def test_empty_posting_defaults_to_english():
    assert cover_note.language_of(_vacancy(None, None)) == "English"


def test_prompt_carries_angle_cv_and_vacancy():
    v = _vacancy("Senior Python Developer", "Build REST APIs.")
    m = MatchResult(best_angle="backend", top_gap="seniority_level", top_gap_confidence=0.7)
    prompt = cover_note.build_prompt(v, m, "MY CV TEXT")
    assert "Write the note in English." in prompt
    assert BEST_ANGLE.criteria["backend"] in prompt
    assert TOP_GAP.criteria["seniority_level"] in prompt
    assert "<cv>\nMY CV TEXT\n</cv>" in prompt
    assert "Build REST APIs." in prompt


def test_prompt_leaves_out_a_vague_gap_and_no_angle():
    v = _vacancy("Dev", "text")
    m = MatchResult(best_angle="none", top_gap="seniority_level", top_gap_confidence=0.17)
    prompt = cover_note.build_prompt(v, m, "cv")
    assert "angle" not in prompt
    assert "weak spot" not in prompt


def test_prompt_works_for_an_unmatched_vacancy():
    assert "<cv>" in cover_note.build_prompt(_vacancy("Dev", "text"), None, "cv")


def test_russian_prompt_asks_for_feminine_forms():
    v = _vacancy("Python-разработчик", "Ищем разработчика на Django.")
    assert "feminine" in cover_note.build_prompt(v, None, "cv")


def test_english_prompt_says_nothing_about_gender():
    v = _vacancy("Python Developer", "We need a Django developer.")
    assert "feminine" not in cover_note.build_prompt(v, None, "cv")
