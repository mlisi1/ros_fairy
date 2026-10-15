"""mission_edit: change a saved mission's briefing, crate kept consistent."""

import io
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from ros_fairy import SCHEMA_VERSION
from ros_fairy.archive import assembler, edit, index
from ros_fairy.manifest import builder
from ros_fairy.manifest.schema import HealthWarning
from ros_fairy.subcommands import mission_edit, verify
from ros_fairy.utils import fsio
from tests.unit.test_archive import _spool


@pytest.fixture
def crate(fairy_dirs):
    harvest, context = _spool(fairy_dirs)
    context["intent"]["notes"] = "calm"
    return assembler.assemble(builder.build(harvest, context), harvest)


def _files(crate):
    return {name: (crate / name).read_text() for name in edit.REWRITTEN}


def _graph(crate):
    doc = json.loads((crate / edit.CRATE).read_text())
    return {e["@id"]: e for e in doc["@graph"]}


def _verify_fails(crate):
    return [c for c in verify.verify_archive(crate) if c["status"] == "fail"]


def test_edit_rewrites_everything_together(crate):
    before = _graph(crate)
    readme_before = (crate / edit.README).read_text()
    changes, final = edit.apply(crate, {"goal": "Survey seagrass beds",
                                        "location_name": "Marsh Creek, south bank",
                                        "notes": "windy"})
    assert final == crate  # a plain edit keeps the folder's name
    assert set(changes) == {"goal", "location_name", "notes"}
    assert changes["goal"].before == "Survey eelgrass beds"

    record = edit.load(crate)
    assert record.intent.goal == "Survey seagrass beds"
    assert record.schema_version == SCHEMA_VERSION
    [made] = record.provenance.edits
    assert made.changes["notes"].before == "calm"
    assert made.changes["notes"].after == "windy"
    assert made.account

    readme = (crate / edit.README).read_text()
    assert readme.startswith("# Survey seagrass beds")
    assert "**Location:** Marsh Creek, south bank" in readme
    assert "**Notes:** windy" in readme
    assert readme_before != readme

    after = _graph(crate)
    assert after["./"]["name"] == "Survey seagrass beds"
    assert "Notes: windy." in after["./"]["description"]
    assert after["#place"]["name"] == "Marsh Creek, south bank"
    assert after["#mission"]["description"] == "Survey seagrass beds"
    assert "dateModified" in after["./"]
    # What can't be rebuilt from the record is kept.
    assert after["./"]["hasPart"] == before["./"]["hasPart"]
    assert after["./"].get("license") == before["./"].get("license")
    assert set(after) == set(before)

    assert _verify_fails(crate) == []
    assert not edit.interrupted(crate)
    [row], _ = index.query(search="seagrass")
    assert row["location"] == "Marsh Creek, south bank"
    assert row["notes"] == "windy"


def test_saved_warnings_survive_and_health_ones_arent_doubled(crate):
    record = edit.load(crate)
    health = "The GPS dropped out for 3 seconds."
    record.bags[0].health_warnings.append(HealthWarning(
        topic="/fix", sensor_id="gps0", kind="gap", plain_text=health))
    readme = assembler._render_readme(record, ["Docker wasn't reachable."])
    assert edit._saved_warnings(readme, record) == ["Docker wasn't reachable."]
    # And through a real edit: the README keeps the saved warning.
    fsio.atomic_write_json(crate / edit.RECORD,
                           record.model_dump(mode="json"))
    (crate / edit.README).write_text(readme)
    (crate / assembler.CHECKSUMS_FILE).unlink()  # hand-made state above
    edit.apply(crate, {"notes": "windy"})
    new = (crate / edit.README).read_text()
    assert new.count("Docker wasn't reachable.") == 1
    assert new.count(health) == 1


def test_edit_shows_in_verify(crate):
    edit.apply(crate, {"notes": "windy"})
    titles = [c["title"] for c in verify.verify_archive(crate)]
    assert any(t.startswith("Briefing edited after saving: 1 time") for t in
               titles)


def test_clearing_an_optional_field(crate):
    edit.apply(crate, {"notes": ""})
    assert edit.load(crate).intent.notes is None
    assert "**Notes:**" not in (crate / edit.README).read_text()
    assert "Notes:" not in _graph(crate)["./"]["description"]


def test_required_fields_cant_be_emptied(crate):
    with pytest.raises(edit.EditError, match="goal can't be empty"):
        edit.apply(crate, {"goal": "  "})


def test_no_change_touches_nothing(crate):
    before = _files(crate)
    assert edit.apply(crate, {"goal": "Survey eelgrass beds"}) == ({}, crate)
    assert _files(crate) == before


def test_a_crate_changed_by_hand_isnt_edited(crate):
    (crate / "harvest" / "harvest.json").write_text("{}")
    before = _files(crate)
    with pytest.raises(edit.EditError, match="harvest/harvest.json"):
        edit.apply(crate, {"goal": "New"})
    assert _files(crate) == before


def test_a_record_from_a_newer_format_isnt_edited(crate):
    data = json.loads((crate / edit.RECORD).read_text())
    major, minor = SCHEMA_VERSION.split(".")
    data["schema_version"] = f"{major}.{int(minor) + 1}"
    data["intent"]["mood"] = "sunny"
    fsio.atomic_write_json(crate / edit.RECORD, data)
    with pytest.raises(edit.EditError, match="newer ros-fairy"):
        edit.load(crate)


def test_an_interrupted_edit_is_finished_when_complete(crate):
    fsio.atomic_write_text(crate / f"{edit.README}{edit.PENDING_SUFFIX}",
                           "# new\n")
    (crate / edit.READY_MARKER).write_text("")
    assert any(c["title"].startswith("An edit of the briefing was "
                                     "interrupted")
               for c in verify.verify_archive(crate))
    assert edit.finish_interrupted(crate) == "finished"
    assert (crate / edit.README).read_text() == "# new\n"
    assert not edit.interrupted(crate)


def test_an_interrupted_edit_is_discarded_when_incomplete(crate):
    before = _files(crate)
    fsio.atomic_write_text(crate / f"{edit.README}{edit.PENDING_SUFFIX}",
                           "# half\n")
    assert edit.finish_interrupted(crate) == "discarded"
    assert _files(crate) == before
    assert not edit.interrupted(crate)


def test_the_files_keep_their_mode(crate):
    (crate / edit.README).chmod(0o664)
    edit.apply(crate, {"notes": "x"})
    assert (crate / edit.README).stat().st_mode & 0o777 == 0o664


# --- the verb --------------------------------------------------------------------

def _run(crate, tty=False, answers=None, **options):
    console = Console(file=io.StringIO(), width=200)
    args = SimpleNamespace(mission=str(crate), yes=True, **options)
    feed = iter(answers or [])
    with mock.patch.object(mission_edit.sys.stdin, "isatty",
                           return_value=tty), \
            mock.patch.object(mission_edit.Prompt, "ask",
                              side_effect=lambda *a, **k: next(feed)):
        code = mission_edit.run(args, console=console)
    return code, " ".join(console.file.getvalue().split())


def test_verb_with_options(crate):
    code, out = _run(crate, location="North bank", notes="")
    assert code == 0
    assert "Updated location, notes" in out
    assert "The folder keeps its name" in out
    assert edit.load(crate).intent.notes is None


def test_verb_interactive_keeps_and_clears(crate):
    # Enter keeps (the prompt returns its default), "-" clears an optional one.
    code, out = _run(crate, tty=True,
                     answers=["Jane Doe", "Better goal", "Marsh Creek",
                              "marine", "-"])
    assert code == 0
    record = edit.load(crate)
    assert record.intent.goal == "Better goal"
    assert record.intent.environment == "marine"
    assert record.intent.notes is None


def test_verb_without_options_or_terminal(crate):
    code, out = _run(crate)
    assert code == 2 and "--goal" in out


def test_verb_nothing_to_change(crate):
    code, out = _run(crate, goal="Survey eelgrass beds")
    assert code == 0 and "Nothing to change" in out


def test_verb_forgets_the_now_stale_export(crate, tmp_path):
    record = edit.load(crate)
    index.mark_exported(record.identity.mission_id, tmp_path / "b.zip", "zip",
                        "abc")
    code, out = _run(crate, notes="later note")
    assert code == 0
    assert "no longer matches" in out and "b.zip" in out
    assert f"-o {tmp_path}/ --force" in out
    assert record.identity.mission_id not in index.exported_mission_ids()


def test_verb_refuses_a_changed_archive(crate):
    (crate / edit.README).write_text("tampered\n")
    code, out = _run(crate, notes="x")
    assert code == 1 and "changed after it was saved" in out


def test_verb_takes_a_mission_id(crate):
    mission_id = edit.load(crate).identity.mission_id
    console = Console(file=io.StringIO(), width=200)
    assert mission_edit.run(SimpleNamespace(mission=mission_id, yes=True,
                                            notes="by id"),
                            console=console) == 0
    assert edit.load(crate).intent.notes == "by id"


# --- redaction -------------------------------------------------------------------

def _everywhere(crate):
    """Every place the briefing text could still be: the crate's files and
    the index file (old pages included)."""
    from ros_fairy.utils import paths
    text = "".join((crate / n).read_text() for n in
                   (edit.RECORD, edit.README, edit.CRATE))
    return text + paths.index_db_path().read_bytes().decode("latin-1")


def test_redact_removes_the_old_values_everywhere(crate):
    edit.apply(crate, {"notes": "call Jane on 555-0199"})
    edit.apply(crate, {"notes": "secret B"})
    assert "555-0199" in _everywhere(crate)  # kept by plain edits
    changes, final = edit.apply(crate, {"notes": "weather fine"},
                                redact=True, by="Ana Admin")
    assert final == crate
    text = _everywhere(final)
    assert "555-0199" not in text and "secret B" not in text
    history = json.dumps(json.loads((final / edit.RECORD).read_text())[
        "provenance"]["edits"])
    assert "calm" not in history  # the value before the first edit, too
    edits = edit.load(final).provenance.edits
    assert [e.redacted for e in edits] == [False, False, True]
    assert edits[-1].by == "Ana Admin" and edits[-1].account
    assert edits[-1].changes["notes"].before is None
    assert edits[-1].changes["notes"].after == "weather fine"
    assert all(e.changes["notes"].redacted and e.changes["notes"].before is
               None and e.changes["notes"].after is None for e in edits[:2])
    assert _verify_fails(final) == []


def test_redacting_the_place_renames_the_folder(crate):
    changes, final = edit.apply(crate, {"location_name": "Somewhere else"},
                                redact=True, by="Ana Admin")
    assert final != crate and not crate.exists()
    assert "somewhere-else" in final.name and "marsh" not in final.name
    [row], _ = index.query()
    assert row["archive_path"] == str(final)
    assert _verify_fails(final) == []


def test_redact_needs_a_name(crate):
    with pytest.raises(edit.EditError, match="name of the person"):
        edit.apply(crate, {"notes": "x"}, redact=True, by="  ")


def test_verb_redact_goes_through_sudo(crate):
    console = Console(file=io.StringIO(), width=200)
    args = SimpleNamespace(mission=str(crate), notes="x", redact=True,
                           by="Ana Admin", yes=True)
    with mock.patch.object(mission_edit.os, "geteuid", return_value=1000), \
            mock.patch.object(mission_edit.shutil, "which",
                              return_value="/usr/bin/sudo"), \
            mock.patch.object(mission_edit.subprocess, "run",
                              return_value=SimpleNamespace(returncode=0)
                              ) as run:
        assert mission_edit.run(args, console=console) == 0
    command = run.call_args.args[0]
    assert command[0] == "/usr/bin/sudo"
    assert command[command.index("--by") + 1] == "Ana Admin"
    assert "--confirmed" in command and "--redact" in command
    assert command[command.index("--notes") + 1] == "x"
    assert "Copies made before" in " ".join(console.file.getvalue().split())
    assert edit.load(crate).intent.notes == "calm"  # the child does it


def test_verb_redact_asks_for_the_name(crate):
    with mock.patch.object(mission_edit.os, "geteuid", return_value=1000), \
            mock.patch.object(mission_edit.shutil, "which",
                              return_value="/usr/bin/sudo"), \
            mock.patch.object(mission_edit.subprocess, "run",
                              return_value=SimpleNamespace(returncode=0)
                              ) as run:
        code, out = _run(crate, tty=True, answers=["", "Ana Admin"],
                         notes="x", redact=True)
    assert code == 0  # an empty name is asked again
    command = run.call_args.args[0]
    assert command[command.index("--by") + 1] == "Ana Admin"


def test_verb_redact_without_name_or_terminal(crate):
    code, out = _run(crate, notes="x", redact=True)
    assert code == 2 and "--by" in out
