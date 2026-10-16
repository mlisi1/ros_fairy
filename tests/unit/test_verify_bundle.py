"""verify on an exported bundle, read without unpacking it."""

import io
import json
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from ros_fairy.archive import bundle
from ros_fairy.subcommands import export, verify
from tests.unit.test_subcommands import _make_archive


def _export(crate: Path, out: Path, fmt: str, compress: bool = False) -> Path:
    fmt, compression = export.choose_format(fmt, compress)
    result = export._export_one(crate, str(out) + "/", fmt, compression,
                                False, Console(file=io.StringIO()))
    assert result["ok"], result
    return Path(result["bundle"])


def _by_title(checks: list[dict]) -> dict[str, str]:
    return {c["title"]: c["status"] for c in checks}


@pytest.fixture
def crate(fairy_dirs):
    return _make_archive(fairy_dirs)


@pytest.mark.parametrize("fmt, compress", [
    ("zip", False), ("zip", True), ("tar", False), ("tar.gz", False),
    ("tar.zst", False)])
def test_a_good_bundle_passes_the_same_checks_as_its_folder(
        crate, tmp_path, fmt, compress):
    path = _export(crate, tmp_path, fmt, compress)
    checks = verify.verify_bundle(path)
    assert checks[0] == {"status": "ok",
                         "title": "The bundle matches its checksum file",
                         "detail": f"{path.name}.sha256"}
    folder = {t: s for t, s in _by_title(verify.verify_archive(crate)).items()
              if "index" not in t}
    assert _by_title(checks[1:]) == folder
    assert verify._overall(checks) != "fail"
    assert any(t.startswith("Recording ") and s == "ok"
               for t, s in folder.items())


def test_the_kind_comes_from_the_content_not_the_name(crate, tmp_path):
    path = _export(crate, tmp_path, "tar.zst")
    renamed = path.rename(tmp_path / "mission.bin")
    assert bundle.kind(renamed) == "tar.zst"
    checks = verify.verify_bundle(renamed)
    assert _by_title(checks)["No checksum file next to the bundle"] == "warn"
    assert verify._overall(checks) != "fail"


def _bag_member(names) -> str:
    return next(n for n in names if "/bags/" in n
                and not n.endswith(("/", "metadata.yaml")))


def test_a_changed_byte_in_a_tar_is_found(crate, tmp_path):
    path = _export(crate, tmp_path, "tar")
    with tarfile.open(path) as tf:
        info = tf.getmember(_bag_member(tf.getnames()))
    data = bytearray(path.read_bytes())
    data[info.offset_data] ^= 0xFF
    path.write_bytes(bytes(data))
    titles = _by_title(verify.verify_bundle(path))
    assert titles["The bundle doesn't match its checksum file"] == "fail"
    assert any(t.startswith("Recording ") and "modified" in t and s == "fail"
               for t, s in titles.items())


def test_a_damaged_zip_member_is_named_and_the_rest_checked(crate,
                                                           tmp_path):
    path = _export(crate, tmp_path, "zip")
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(_bag_member(zf.namelist()))
    data = bytearray(path.read_bytes())
    # The stored data starts after the local header (30 bytes + name +
    # extra field).
    with open(path, "rb") as f:
        f.seek(info.header_offset + 26)
        name_len, extra_len = (int.from_bytes(f.read(2), "little")
                               for _ in range(2))
    data[info.header_offset + 30 + name_len + extra_len] ^= 0xFF
    path.write_bytes(bytes(data))
    checks = verify.verify_bundle(path)
    titles = _by_title(checks)
    assert titles["Some files in the bundle can't be read"] == "fail"
    assert titles["Mission record is valid"] == "ok"
    assert titles["The mission's descriptive files match their checksums"] \
        == "ok"


def test_a_cut_short_bundle_is_damaged(crate, tmp_path):
    path = _export(crate, tmp_path, "tar.zst")
    path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])
    [check] = verify.verify_bundle(path)
    assert check["status"] == "fail"
    assert "damaged or cut short" in check["detail"]


def test_a_file_that_is_no_bundle(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    [check] = verify.verify_bundle(path)
    assert check == {"status": "fail", "title": "The bundle can't be read",
                     "detail": "it isn't a .zip or .tar bundle"}


def _tar_with(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def test_more_than_one_top_folder_is_refused(tmp_path):
    path = _tar_with(tmp_path / "x.tar", {"a/mission_record.json": b"{}",
                                          "b/README.md": b""})
    [check] = verify.verify_bundle(path)
    assert "one folder at the top, this has 2" in check["detail"]


def test_a_path_outside_the_bundle_is_refused(tmp_path):
    path = _tar_with(tmp_path / "x.tar", {"a/../../etc/passwd": b""})
    [check] = verify.verify_bundle(path)
    assert "outside the bundle" in check["detail"]


def test_a_file_added_inside_the_bundle_is_noted(crate, tmp_path):
    (crate / "extra.txt").write_text("added by hand")
    path = _export(crate, tmp_path, "zip")
    titles = _by_title(verify.verify_bundle(path))
    assert titles["Files were added to the archive after it was saved"] \
        == "warn"


def test_verify_takes_a_bundle_on_the_command_line(crate, tmp_path, capsys):
    path = _export(crate, tmp_path, "tar.gz")
    assert verify.run(SimpleNamespace(mission=str(path), json=True)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["bundle"] is True and data["archive"] == str(path)
    assert data["result"] in ("ok", "warn")

    path.write_bytes(path.read_bytes()[:-100])
    console = Console(file=io.StringIO(), width=120)
    assert verify.run(SimpleNamespace(mission=str(path)),
                      console=console) == 1
    assert "can't be read" in console.file.getvalue()


def test_a_folder_still_reports_the_index(crate, capsys):
    assert verify.run(SimpleNamespace(mission=str(crate), json=True)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["bundle"] is False
    assert "Mission is registered in the index" in \
        [c["title"] for c in data["checks"]]
