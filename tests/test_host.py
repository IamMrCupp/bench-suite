"""The real :class:`bench_setup.host.Host`: the parts that need no root.

Running commands as another user, and systemd itself, are covered by the
end-to-end install test on a clean Ubuntu container.
"""

from __future__ import annotations

import http.server
import io
import json
import sys
import threading

import pytest

from bench_setup.host import Host
from bench_setup.services import TerminalUI


def test_read_settings_parses_what_the_services_write(tmp_path) -> None:
    path = tmp_path / "settings.env"
    path.write_text(
        "# bench-parts settings. Environment variables override these.\n"
        "BENCH_PARTS_PORT=8402\n"
        "\n"
        "  BENCH_PARTS_API_TOKEN = abc=def \n"
        "not a setting\n",
        "utf-8",
    )
    assert Host().read_settings(path) == {
        "BENCH_PARTS_PORT": "8402",
        "BENCH_PARTS_API_TOKEN": "abc=def",
    }


def test_a_missing_settings_file_is_empty(tmp_path) -> None:
    assert Host().read_settings(tmp_path / "nope.env") == {}


def test_read_toml_reads_benchhuds_sections(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[intake]\nbase_url = "http://127.0.0.1:8401"\ntoken = "t"\n', "utf-8")
    assert Host().read_toml(path)["intake"] == {"base_url": "http://127.0.0.1:8401", "token": "t"}


@pytest.mark.parametrize("content", [None, "this is [not toml"])
def test_a_missing_or_broken_config_is_empty(tmp_path, content) -> None:
    path = tmp_path / "config.toml"
    if content is not None:
        path.write_text(content, "utf-8")
    assert Host().read_toml(path) == {}


def test_run_pipes_stdin_and_returns_stdout_and_the_exit_code() -> None:
    code, out, err = Host().run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper()); sys.exit(3)"],
        stdin="secret",
    )
    assert (code, out.strip(), err) == (3, "SECRET", "")


class _Health(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def test_healthz_returns_the_body(server, monkeypatch) -> None:
    # A proxy in the environment must not be used for loopback.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    _Health.body = json.dumps({"status": "ok", "app": "bench-parts", "version": "0.4.0"}).encode()
    assert Host().healthz(server.server_port)["app"] == "bench-parts"


@pytest.mark.parametrize("body", [b"not json", b'["a list"]'])
def test_healthz_that_isnt_a_json_object_is_none(server, body) -> None:
    _Health.body = body
    assert Host().healthz(server.server_port) is None


def test_healthz_with_nothing_listening_is_none(server) -> None:
    port = server.server_port
    server.shutdown()
    server.server_close()
    assert Host().healthz(port) is None


def test_sudo_user_ignores_root(monkeypatch) -> None:
    monkeypatch.setenv("SUDO_USER", "root")
    assert Host().sudo_user() is None
    monkeypatch.setenv("SUDO_USER", "aaron")
    assert Host().sudo_user() == "aaron"
    monkeypatch.delenv("SUDO_USER")
    assert Host().sudo_user() is None


def test_scripted_password_is_one_line_of_stdin(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("hunter2 with spaces\r\nnext line\n"))
    assert TerminalUI(interactive=False).ask_password() == "hunter2 with spaces"


def test_scripted_password_missing_is_none(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert TerminalUI(interactive=False).ask_password() is None


def test_scripted_runs_never_say_yes(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("prompted"))
    assert TerminalUI(interactive=False).ask_yes_no("Turn on bench-recon?") is False


def _typed(monkeypatch, *answers: str) -> list[str]:
    prompts: list[str] = []
    replies = iter(answers)

    def fake(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr("getpass.getpass", fake)
    return prompts


def test_the_prompt_asks_twice_and_returns_a_match(monkeypatch) -> None:
    prompts = _typed(monkeypatch, "pw one", "pw one")
    assert TerminalUI(interactive=True).ask_password() == "pw one"
    assert len(prompts) == 2


def test_a_mismatch_or_a_blank_asks_again_then_gives_up(monkeypatch, capsys) -> None:
    _typed(monkeypatch, "a", "b", "  ", "c", "c")
    assert TerminalUI(interactive=True).ask_password() == "c"
    err = capsys.readouterr().err
    assert "didn't match" in err and "can't be blank" in err

    _typed(monkeypatch, "a", "b", "a", "b", "a", "b")
    assert TerminalUI(interactive=True).ask_password() is None


@pytest.mark.parametrize(
    ("typed", "expected"), [("y", True), ("YES", True), ("", False), ("n", False)]
)
def test_yes_no_defaults_to_no(monkeypatch, typed, expected) -> None:
    monkeypatch.setattr("builtins.input", lambda _: typed)
    assert TerminalUI(interactive=True).ask_yes_no("Turn on bench-recon?") is expected
