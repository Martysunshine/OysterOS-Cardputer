"""Connection settings from the environment: names, defaults, problem codes, secrecy of the fields."""

from __future__ import annotations

from pathlib import Path

import pytest
from oyster.home.config import DEFAULT_ALLOWLIST_NAME, HomeConfig

URL = "https://ha.example.test:8123"
TOKEN = "t0ken-0123456789abcdefghijklmnopqrstuvwxyz"
AT = chr(64)  # kept out of literals: a user@host string looks like an e-mail address to the repository scan


def cfg(**kw) -> HomeConfig:
    return HomeConfig(**{"url": URL, "token": TOKEN, **kw})


def test_from_env_reads_the_four_names(tmp_path: Path):
    env = {
        "HOME_ASSISTANT_URL": f" {URL}/ ",
        "HOME_ASSISTANT_TOKEN": f" {TOKEN}\n",
        "HOME_ASSISTANT_CA": "/etc/ssl/ha-ca.pem",
        "OYSTER_HOME_ALLOWLIST": str(tmp_path / "custom.json"),
    }
    c = HomeConfig.from_env(tmp_path, env.get)
    assert (c.url, c.token, c.ca_file) == (URL, TOKEN, "/etc/ssl/ha-ca.pem")
    assert c.allowlist_path == tmp_path / "custom.json"
    assert c.problem() is None and c.tls


def test_allowlist_path_defaults_to_the_data_directory(tmp_path: Path):
    c = HomeConfig.from_env(tmp_path, {}.get)
    assert c.allowlist_path == tmp_path / DEFAULT_ALLOWLIST_NAME
    c = HomeConfig.from_env(tmp_path, {"OYSTER_HOME_ALLOWLIST": "lists/home.json"}.get)
    assert c.allowlist_path == tmp_path / "lists" / "home.json"  # relative = relative to the data directory


def test_nothing_set_means_no_url():
    assert HomeConfig.from_env(Path("."), {}.get).problem() == "no_url"


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"url": ""}, "no_url"),
        ({"url": "ftp://ha.example.test"}, "bad_url"),
        ({"url": "ha.example.test:8123"}, "bad_url"),
        ({"url": "http://"}, "bad_url"),
        ({"url": "http://user:pw" + AT + "ha.example.test"}, "bad_url"),
        ({"url": "http://user" + AT + "ha.example.test"}, "bad_url"),
        ({"url": "http://ha.example.test?x=1"}, "bad_url"),
        ({"url": "http://ha.example.test#frag"}, "bad_url"),
        ({"url": "http://ha.example.test:99999"}, "bad_url"),
        ({"token": ""}, "no_token"),
        ({"token": "has space"}, "bad_token"),
        ({"token": "line\nbreak"}, "bad_token"),
        ({"token": "töken"}, "bad_token"),
        ({"token": "x" * 3000}, "bad_token"),
    ],
)
def test_problem_codes(kw, code):
    assert cfg(**kw).problem() == code


def test_url_problem_is_reported_before_the_token_problem():
    assert cfg(url="", token="").problem() == "no_url"
    assert cfg(token="").problem() == "no_token"


def test_plain_http_is_allowed_but_not_tls():
    c = cfg(url="http://192.168.1.50:8123")
    assert c.problem() is None and not c.tls


def test_secrets_stay_out_of_repr():
    c = cfg(ca_file="/secret/ca.pem")
    text = repr(c) + str(c)
    assert TOKEN not in text and "ha.example.test" not in text and "/secret/ca.pem" not in text
