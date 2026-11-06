"""Connection identity and credential rules do not require a database server."""

import importlib.util
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
NAME = "storage_mysql_contract_test"
spec = importlib.util.spec_from_file_location(NAME, ROOT / "plugin.py", submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[NAME] = module
spec.loader.exec_module(module)
from storage_mysql_contract_test.database import MysqlSettings, mysql_connection_options
from storage_mysql_contract_test.provider import MysqlStorageProvider


class Credentials:
    def __init__(self, password):
        self.password = password
    def get(self, key):
        assert key == "password"
        return self.password


def test_credentials_do_not_participate_in_target_identity():
    settings = {"host": "database.example", "database": "magi", "user": "owner"}
    first = MysqlStorageProvider(settings, Credentials("first-secret"))
    second = MysqlStorageProvider(settings, Credentials("second-secret"))
    assert first.target_identity == second.target_identity
    assert first._database is second._database is None
    assert "first-secret" not in repr(first.settings)
    with pytest.raises(ValueError, match="not configured"):
        MysqlStorageProvider(settings, Credentials(None))


def test_remote_tls_and_empty_optional_ca_are_explicit():
    settings = MysqlSettings(host="database.example", database="magi", user="owner", password="secret", ca_file="")
    assert settings.ca_file is None
    _, options = mysql_connection_options(settings, "asyncmy")
    assert options["ssl"].check_hostname and not options["local_infile"]
    with pytest.raises(ValueError, match="require TLS"):
        mysql_connection_options(settings.model_copy(update={"tls": False}), "asyncmy")


def test_settings_reject_undeclared_connection_keys():
    with pytest.raises(ValueError):
        MysqlStorageProvider({"host": "localhost", "database": "magi", "user": "owner", "unexpected": True}, Credentials("secret"))
