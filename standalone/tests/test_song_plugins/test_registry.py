"""Plugin registry: discovery, manifest validation, duplicate handling."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from standalone.song_plugins.api import (
    PluginManifest, ParamSpec, SongPlugin, PluginResult,
)
from standalone.song_plugins.registry import (
    validate_manifest, load_builtin_plugins, load_plugins_from_dir,
)


def test_load_builtin_includes_note_density():
    plugins = load_builtin_plugins()
    assert "builtin.note_density" in plugins
    # And its manifest is valid.
    validate_manifest(plugins["builtin.note_density"].manifest)


def test_manifest_selection_without_kinds_rejected():
    m = PluginManifest(
        id="x", name="x", version="1", description="",
        capabilities=("analyze",),
        scopes=("selection",),
        selection_kinds=(),
    )
    with pytest.raises(ValueError, match="selection"):
        validate_manifest(m)


def test_manifest_empty_capabilities_rejected():
    m = PluginManifest(
        id="x", name="x", version="1", description="",
        capabilities=(),
    )
    with pytest.raises(ValueError, match="capabilities"):
        validate_manifest(m)


def test_duplicate_plugin_id_skipped(tmp_path: Path, caplog):
    body = textwrap.dedent('''
        from standalone.song_plugins.api import (
            SongPlugin, PluginManifest, PluginResult,
        )
        class P(SongPlugin):
            manifest = PluginManifest(
                id="dup.id", name="P", version="1",
                description="", capabilities=("analyze",),
            )
            def run(self, view, params, progress):
                return PluginResult()
        PLUGIN = P
    ''').strip() + "\n"
    (tmp_path / "a.py").write_text(body)
    (tmp_path / "b.py").write_text(body)

    with caplog.at_level("WARNING"):
        plugins = load_plugins_from_dir(tmp_path)
    assert len(plugins) == 1
    assert "dup.id" in plugins
    # One warning about the duplicate.
    assert any("Duplicate plugin id" in r.message for r in caplog.records)


def test_plugin_import_error_skipped(tmp_path: Path, caplog):
    (tmp_path / "broken.py").write_text("import nonexistent_module_xyz\n")
    with caplog.at_level("WARNING"):
        plugins = load_plugins_from_dir(tmp_path)
    assert plugins == {}
    assert any("Failed to import" in r.message for r in caplog.records)


def test_plugin_without_manifest_skipped(tmp_path: Path, caplog):
    body = textwrap.dedent('''
        from standalone.song_plugins.api import SongPlugin, PluginResult
        class P(SongPlugin):
            def run(self, view, params, progress):
                return PluginResult()
        PLUGIN = P
    ''').strip() + "\n"
    (tmp_path / "p.py").write_text(body)
    with caplog.at_level("WARNING"):
        plugins = load_plugins_from_dir(tmp_path)
    assert plugins == {}


def test_enum_param_requires_choices():
    m = PluginManifest(
        id="x", name="x", version="1", description="",
        capabilities=("analyze",),
        params=(ParamSpec(key="k", type="enum", label="K", default=None),),
    )
    with pytest.raises(ValueError, match="choices"):
        validate_manifest(m)
