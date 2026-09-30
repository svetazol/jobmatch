"""Loading config.toml, and the guards that stop it lying.

No database and no network: `load_settings` is pure, so these run on written
files in tmp_path. The real `config.toml` is exercised too -- it is checked in,
so a change that breaks it should break the suite rather than the next crawl.
"""
import pytest

from jobmatch.config import DEFAULT_PATH, load_settings

MINIMAL = """
[sources."hh.ru".search]
text = "python"
[sources."hh.ru".sweeps]
all = ["georgia"]
"""


def write(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --- what a config does not have to say -------------------------------------

def test_a_config_that_states_no_limits_defers_to_the_source(tmp_path):
    """hh's measured rate and ceiling are hh's, so a config silent about them is
    complete, not incomplete."""
    settings = load_settings(write(tmp_path, MINIMAL))
    assert (settings.fetch_workers, settings.fetch_delay, settings.crawl) == (None, None, None)


def test_the_source_rate_is_used_unless_the_config_overrides_it(tmp_path):
    from jobmatch.sources.hh import RATE

    settings = load_settings(write(tmp_path, MINIMAL))
    assert settings.rate_for(RATE) == RATE              # nothing stated -> hh's own

    louder = load_settings(write(tmp_path, "fetch_workers = 1\n" + MINIMAL))
    assert louder.rate_for(RATE).workers == 1           # the override wins
    assert louder.rate_for(RATE).delay == RATE.delay    # the rest stays hh's


# --- sweeps ------------------------------------------------------------------

def test_sweeps_become_the_crawl_plan(tmp_path):
    body = MINIMAL + 'ru = ["russia"]\n'
    plan = load_settings(write(tmp_path, body)).crawls()
    assert [(sweep, countries) for _, sweep, countries in plan] == [
        ("all", ("georgia",)),
        ("ru", ("russia",)),
    ]


def test_only_narrows_to_one_sweep_and_keeps_everything_else(tmp_path):
    """The bug that retired config.bg.toml: a second file for one different
    sweep let `fetch_workers` drift to a value the first file called unsafe."""
    body = 'model = "jev-9.9"\n' + MINIMAL + 'ru = ["russia"]\n'
    settings = load_settings(write(tmp_path, body))
    narrowed = settings.only("all")
    assert [c for _, _, c in narrowed.crawls()] == [("georgia",)]
    assert narrowed.model == settings.model == "jev-9.9"
    assert narrowed.sources[0].search == settings.sources[0].search


def test_an_unknown_sweep_says_which_ones_exist(tmp_path):
    settings = load_settings(write(tmp_path, MINIMAL))
    with pytest.raises(ValueError, match="no sweep named 'bg'.*all"):
        settings.only("bg")


def test_a_source_with_no_sweeps_is_refused(tmp_path):
    body = '[sources."hh.ru".search]\ntext = "python"\n'
    with pytest.raises(ValueError, match="defines no sweeps"):
        load_settings(write(tmp_path, body))


# --- failing loudly ----------------------------------------------------------

def test_a_config_with_no_sources_is_refused(tmp_path):
    with pytest.raises(ValueError, match="enables no sources"):
        load_settings(write(tmp_path, "model = 'jev-1.13'\n"))


def test_a_misspelled_setting_is_named_rather_than_ignored(tmp_path):
    """`fetch_worker` used to run one worker in silence, making the crawl three
    times slower than the rate the comments justify."""
    with pytest.raises(ValueError, match="unknown setting.*fetch_worker"):
        load_settings(write(tmp_path, "fetch_worker = 6\n" + MINIMAL))


def test_an_unknown_source_is_caught_at_load(tmp_path):
    body = '[sources."linkedin".search]\ntext = "python"\n[sources."linkedin".sweeps]\na = ["russia"]\n'
    with pytest.raises(ValueError, match="unknown source.*linkedin"):
        load_settings(write(tmp_path, body))


def test_a_stray_key_under_a_source_is_caught(tmp_path):
    """Notably `params`, which is what this used to be called."""
    body = MINIMAL + '\n[sources."hh.ru".params]\narea = [1]\n'
    with pytest.raises(ValueError, match="unknown key.*params"):
        load_settings(write(tmp_path, body))


def test_a_mistyped_country_fails_before_any_request(tmp_path):
    """Otherwise it surfaces partway through a crawl, after the earlier sweeps
    have already spent their requests."""
    body = MINIMAL.replace('["georgia"]', '["gorgia"]')
    with pytest.raises(ValueError, match="unknown country 'gorgia'"):
        load_settings(write(tmp_path, body))


def test_a_sweep_given_a_bare_string_is_refused(tmp_path):
    """`ru = "russia"` would otherwise iterate the characters."""
    body = MINIMAL.replace('["georgia"]', '"georgia"')
    with pytest.raises(ValueError, match="must be a non-empty list"):
        load_settings(write(tmp_path, body))


# --- the checked-in file -----------------------------------------------------

def test_the_checked_in_config_loads_and_holds_only_choices():
    settings = load_settings(DEFAULT_PATH)
    assert {s for _, s, _ in settings.crawls()} == {"bg", "ru"}
    assert (settings.crawl, settings.fetch_workers, settings.fetch_delay) == (None, None, None)
    assert settings.model and settings.cv_path and settings.stats_cv_hash


def test_the_checked_in_config_expands_to_the_queries_it_used_to_list():
    """Its six blocks and 88 hand-typed ids became two country names. The crawl
    must be the same size: 2 for bg, 92 for ru."""
    from jobmatch.sources.hh import areas

    plan = {sweep: countries for _, sweep, countries in load_settings(DEFAULT_PATH).crawls()}
    assert sum(len(areas.coverage(c)) for c in plan["bg"]) == 2
    assert sum(len(areas.coverage(c)) for c in plan["ru"]) == 92
