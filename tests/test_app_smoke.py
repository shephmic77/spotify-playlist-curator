"""Run the Streamlit script top to bottom against a stand-in ``streamlit`` module.

This doesn't render anything; it catches typos, wrong arguments and broken data plumbing in
every tab. Buttons report "not clicked", inputs return their defaults.
"""

from __future__ import annotations

import runpy
import sys
import types
from pathlib import Path

import pytest

from library_builder import build_library
from playlist_curator.curator import discovery as disc
from playlist_curator.curator import intents as it
from playlist_curator.storage.database import Database

APP = Path(__file__).parents[1] / "src" / "playlist_curator" / "app.py"


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return getattr(FAKE_ST, name)


def _fake_streamlit() -> types.ModuleType:
    st = types.ModuleType("streamlit")
    calls: list[str] = []

    def record(name):
        def fn(*args, **kwargs):
            calls.append(name)
            return None

        return fn

    for name in (
        "set_page_config",
        "header",
        "title",
        "subheader",
        "caption",
        "markdown",
        "write",
        "info",
        "warning",
        "success",
        "error",
        "json",
        "dataframe",
        "metric",
    ):
        setattr(st, name, record(name))
    st.button = lambda *a, **k: False
    st.form_submit_button = lambda *a, **k: False
    st.checkbox = lambda label, value=False, **k: value
    st.text_input = lambda label, value="", **k: value
    st.text_area = lambda label, value="", **k: value
    st.selectbox = lambda label, options, index=0, **k: list(options)[index]

    def data_editor(data, **k):
        calls.append("data_editor")
        return data

    st.data_editor = data_editor
    st.column_config = types.SimpleNamespace(SelectboxColumn=lambda **k: None)

    def columns(spec, **k):
        n = spec if isinstance(spec, int) else len(spec)
        return [_Ctx() for _ in range(n)]

    st.columns = columns
    st.tabs = lambda labels: [_Ctx() for _ in labels]
    st.expander = lambda *a, **k: _Ctx()
    st.container = lambda *a, **k: _Ctx()
    st.form = lambda *a, **k: _Ctx()
    st.status = lambda *a, **k: _Ctx()
    st.sidebar = _Ctx()
    st.session_state = {}

    def stop():
        raise SystemExit

    st.stop = stop
    st.rerun = stop
    st.calls = calls
    return st


FAKE_ST = _fake_streamlit()


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    pytest.importorskip("pandas")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "x")
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setitem(sys.modules, "streamlit", FAKE_ST)
    FAKE_ST.calls.clear()
    return tmp_path


def test_app_renders_without_data(app_env):
    with pytest.raises(SystemExit):  # st.stop() when nothing is imported yet
        runpy.run_path(str(APP), run_name="__main__")
    assert "info" in FAKE_ST.calls


def test_app_renders_every_tab_with_a_library(app_env, monkeypatch):
    with Database(app_env / "data" / "curator.sqlite3") as db:
        desc = build_library(db)
        it.set_intent(db, "W", "gym", "situation", "Workout")
        it.set_intent(db, "RS", "show", "handpicked", "Regular Show")
        monkeypatch.setattr(disc, "load_descriptors", lambda _db: desc)
        props, _ = disc.propose(db, descriptors=desc)
        disc.save_proposals(db, props)
        disc.accept(db, disc.load_proposals(db)[0].proposal_id)
        from playlist_curator.curator import scoring as sc

        monkeypatch.setattr(sc, "load_descriptors", lambda _db: desc)
        sc.build_drafts(db, descriptors=desc)
    runpy.run_path(str(APP), run_name="__main__")
    assert FAKE_ST.calls.count("dataframe") >= 5  # includes draft overview + progress
    assert FAKE_ST.calls.count("data_editor") >= 1  # editable draft table
    assert "metric" in FAKE_ST.calls
