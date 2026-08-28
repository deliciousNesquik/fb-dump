import pytest

from fb_dump import config


def test_flags_win_over_env():
    s = config.resolve({"FB_DATABASE": "envdb", "ISC_USER": "ENVU", "ISC_PASSWORD": "pw", "FB_CHARSET": "win1251", "FB_ROLE": "ENVR"},
                       database="flagdb", user="FLAGU", role="FLAGR", charset="utf-8")
    assert (s.database, s.user, s.password, s.role, s.charset, s.fallback_charset) == ("flagdb", "FLAGU", "pw", "FLAGR", "UTF8", None)


def test_env_only_and_defaults():
    s = config.resolve({"FB_DATABASE": "host:alias", "FB_ROLE": "RDONLY"})
    assert (s.database, s.user, s.password, s.role, s.charset) == ("host:alias", None, None, "RDONLY", "UTF8")


def test_missing_database():
    with pytest.raises(config.ConfigError):
        config.resolve({})
    with pytest.raises(config.ConfigError):
        config.resolve({"FB_DATABASE": "  "})


def test_fallback_must_differ():
    with pytest.raises(config.ConfigError):
        config.resolve({"FB_DATABASE": "x"}, charset="utf8", fallback_charset="UTF-8")
    assert config.resolve({"FB_DATABASE": "x"}, fallback_charset="win1251").fallback_charset == "WIN1251"


def test_isolation_default_flag_and_env():
    assert config.resolve({"FB_DATABASE": "x"}).isolation == "read-committed"
    assert config.resolve({"FB_DATABASE": "x", "FB_ISOLATION": "snapshot"}).isolation == "snapshot"
    assert config.resolve({"FB_DATABASE": "x", "FB_ISOLATION": "snapshot"}, isolation="read-committed").isolation == "read-committed"
    assert config.resolve({"FB_DATABASE": "x"}, isolation=" SNAPSHOT ").isolation == "snapshot"


def test_unknown_isolation_is_a_config_error():
    with pytest.raises(config.ConfigError, match="unknown isolation"):
        config.resolve({"FB_DATABASE": "x"}, isolation="serializable")
    with pytest.raises(config.ConfigError):
        config.resolve({"FB_DATABASE": "x", "FB_ISOLATION": "repeatable-read"})
