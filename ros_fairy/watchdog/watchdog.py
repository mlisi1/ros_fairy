"""The ros-fairy watchdog: the always-on "dashcam" context recorder.

Watches the spool bag directory via inotify, harvests context when a recording
starts, finalises bag records when it stops. Never archives — that is the
operator's decision at
``ros2 fairy mission_close``.

Testability: the inotify object and the clock are injectable, and
``run_pipeline``/``step`` are callable synchronously, so tests drive the state
machine with fabricated events (no real ROS, Docker, or robot needed).
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ros_fairy.manifest import builder
from ros_fairy.utils import fsio, paths, ros_env, topic_health
from ros_fairy.watchdog import recorder_scan

log = logging.getLogger("ros_fairy.watchdog")

BAG_INACTIVITY_S = 30
DOCKER_TIMEOUT_S = 10
PIP_TIMEOUT_S = 30
HARDWARE_CMD_TIMEOUT_S = 10
HARDWARE_TOTAL_TIMEOUT_S = 60
ROS_RETRY_INTERVAL_S = 60
HEARTBEAT_S = 60
# How often /proc is scanned for recorders (~6 ms per scan on Jo). A
# recorder seen once before it wrote anything is caught even if it ends
# before the next scan (Watchdog._pending); one whose whole life fits between
# two scans is not, and a recorder node takes about a second just to start.
FOREIGN_SCAN_INTERVAL_S = 1
# A robot that publishes no URDF/static TF: retry the harvest for it only this
# many times per recording.
MAX_DESCRIPTION_RETRIES = 2
# A failing status-file write is logged at most this often.
STATE_ERROR_LOG_S = 300
# Upper bound on waiting for an in-flight harvest at finalise time. The
# pipeline's own module timeouts sum to well under this (the ROS snapshot is
# hard-killed after ~50 s); past this the harvest is considered hung and the
# bag is finalised with whatever is on disk.
HARVEST_WAIT_S = 240

STORAGE_SUFFIXES = (".db3", ".mcap")

IDLE, RECORDING, FINALISING = "IDLE", "RECORDING", "FINALISING"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_storage_file(name: str) -> bool:
    return name.endswith(STORAGE_SUFFIXES)


def run_pipeline() -> dict[str, Any]:
    """Run all harvest modules in spec order; never raises.

    Returns the composed harvest.json document (without bags).
    """
    from ros_fairy.harvest import (
        docker_info,
        hardware_devices,
        python_env,
        robot_identity,
        ros_graph,
        system_info,
    )

    status: dict[str, str] = {}
    results: dict[str, Any] = {}

    def attempt(name: str, fn: Callable[[], dict]) -> None:
        try:
            results[name] = fn()
            status[name] = "ok"
        except Exception as exc:
            results[name] = None
            status[name] = "failed"
            log.warning("harvest module %s failed: %s", name, exc)

    attempt("robot_identity", robot_identity.harvest)
    attempt("system_info", system_info.harvest)

    attempt("python_env", python_env.harvest)
    if status["python_env"] == "ok":
        status["python_env"] = (results["python_env"] or {}).get("status", "ok")

    attempt("hardware_devices", hardware_devices.harvest)
    if status["hardware_devices"] == "ok":
        status["hardware_devices"] = \
            (results["hardware_devices"] or {}).get("status", "ok")

    attempt("ros_graph", ros_graph.harvest)
    if status["ros_graph"] == "ok" and not results["ros_graph"]["complete"]:
        status["ros_graph"] = "partial"
    # The URDF and static transforms come from the same single-participant
    # snapshot; a failed snapshot is "failed" (its reason is logged above),
    # not a "timeout" that hides why (2026-10-01).
    descriptions = None
    if results["ros_graph"] is None:
        status["ros_descriptions"] = "failed"
    else:
        descriptions = {k: results["ros_graph"].pop(k, None)
                        for k in ("robot_description", "tf_static")}
        publishers = results["ros_graph"].pop("description_publishers",
                                              None) or {}
        if descriptions["robot_description"] or descriptions["tf_static"]:
            status["ros_descriptions"] = "ok"
        elif publishers and not any(publishers.values()):
            # Nobody publishes a URDF or static transforms: a known answer,
            # not a capture that timed out, and nothing a retry would fix.
            status["ros_descriptions"] = "absent"
        else:
            status["ros_descriptions"] = "timeout"
    if results["ros_graph"] is None:
        # `ros2 pkg list` reads the local install, no DDS involved: a failed
        # discovery must not cost the mission its installed-package record.
        try:
            results["ros_graph"] = {"ros_packages": ros_graph.list_packages()}
        except Exception as exc:
            log.warning("listing installed ROS packages failed: %s", exc)
    attempt("docker_info", docker_info.harvest)
    if status["docker_info"] == "ok" and \
            not results["docker_info"]["available"]:
        status["docker_info"] = "skipped"

    return builder.compose_harvest(
        identity=results["robot_identity"],
        system=results["system_info"],
        graph=results["ros_graph"],
        docker=results["docker_info"],
        descriptions=descriptions,
        harvest_status=status,
        python_env=results["python_env"],
        hardware_devices=results["hardware_devices"],
    )


_STATUS_RANK = {"ok": 3, "partial": 2}  # anything else: nothing captured

# Which parts of harvest.json each ROS module produces. Only these modules are
# retried, so only they can be clobbered by a later, worse run.
_ROS_MODULE_FIELDS = {
    "ros_graph": ("captured_at", "nodes", "topics", "parameters",
                  "parameters_not_captured", "complete"),
    "ros_descriptions": ("robot_description", "tf_static"),
}


def _keep_better_ros_capture(existing: dict, new: dict,
                             prefer_existing: tuple[str, ...] = ()) -> dict:
    """``new`` with each ROS module's result replaced by ``existing``'s where
    this run captured nothing for it and the earlier one did.

    Harvests repeat (one per recording, plus retries), and a later one can run
    after the stack went down: its empty result used to overwrite a good
    capture wholesale, so the mission was archived with no graph (2026-10-01).
    Modules in ``prefer_existing`` keep an earlier capture even when this run
    captured one too (a retry that only wanted the robot description must
    not swap the recording's graph for a later one).
    """
    old_status = existing.get("provenance", {}).get("harvest_status", {})
    new_status = new.get("provenance", {}).get("harvest_status", {})
    merged = {**new, "ros_graph": dict(new.get("ros_graph") or {}),
              "provenance": {**new.get("provenance", {}),
                             "harvest_status": dict(new_status)}}
    for module, fields in _ROS_MODULE_FIELDS.items():
        # Only an empty-handed run defers to an earlier capture. Ranking two
        # real captures against each other is wrong — they may be different
        # graphs entirely (2026-10-02: a stale 4-node "ok" test capture beat
        # the operator's live 38-node "partial" one).
        captured_before = _STATUS_RANK.get(old_status.get(module), 0) > 0
        captured_now = _STATUS_RANK.get(new_status.get(module), 0) > 0 \
            and module not in prefer_existing
        if captured_now or not captured_before:
            continue
        for field in fields:
            merged["ros_graph"][field] = (existing.get("ros_graph") or {}).get(
                field)
        merged["provenance"]["harvest_status"][module] = old_status[module]
        if module == "ros_graph":
            # sensor liveness was derived from that same graph
            merged["sensors"] = existing.get("sensors", new.get("sensors"))
            log.info("kept the earlier ROS graph capture (%s) over this "
                     "run's (%s)", old_status[module],
                     new_status.get(module))
    # Installed packages: keep a captured list over a missing one.
    old_pkgs = (existing.get("software") or {}).get("ros_packages")
    if old_pkgs and not (new.get("software") or {}).get("ros_packages"):
        merged["software"] = {**new.get("software", {}),
                              "ros_packages": old_pkgs}
    return merged


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _newest_mtime(bag_dir: Path) -> float | None:
    try:
        return max((f.stat().st_mtime for f in bag_dir.iterdir()),
                   default=None)
    except OSError:
        return None


def _spool_lock():
    """Serialises read-modify-write updates of harvest.json across the
    watchdog's threads and the operator CLIs (adopt, mission_close)."""
    return fsio.locked(paths.harvest_lock_path())


def _load_harvest_for_update() -> dict | None:
    """harvest.json for an update, or None if there is none.

    An unparseable file is moved aside (``harvest.json.corrupt-<time>``)
    rather than silently overwritten: it may be the only record of the
    mission's earlier recordings. Call with ``_spool_lock()`` held.
    """
    path = paths.harvest_json_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        aside = path.with_name(f"harvest.json.corrupt-{stamp}")
        os.replace(path, aside)
        log.warning("harvest.json was unreadable; kept it as %s and started "
                    "a new one", aside.name)
        return None


class Watchdog:
    def __init__(self, inotify=None, clock: Callable[[], float] = time.monotonic,
                 pipeline: Callable[[], dict] = run_pipeline,
                 harvest_in_thread: bool = True,
                 scan_recorders: Callable[[], list] = recorder_scan.scan):
        if inotify is None:
            from inotify_simple import INotify
            inotify = INotify()
        self.ino = inotify
        self.clock = clock
        self.pipeline = pipeline
        self.harvest_in_thread = harvest_in_thread
        # Injected so tests drive foreign-bag detection without real processes.
        self.scan_recorders = scan_recorders

        self.state = IDLE
        self.since = _now_iso()
        self.active_bag_dir: Path | None = None
        self.queued_bags: list[Path] = []
        # Bag dirs recorded outside mission_record (the /proc poller found them):
        # path -> {"pid", "start", "discovery"}. Drives in-place referencing,
        # environ adoption, and the "detected" source tag at finalise.
        self._foreign: dict[Path, dict] = {}
        # Recorder processes writing spool bags (mission_record), found by the
        # same poller: path -> {"pid", "start"}. A quiet spool bag is not a
        # finished one while its recorder runs (MCAP buffers whole chunks).
        self._recorders: dict[Path, dict] = {}
        # Recorders whose output folder is known but still empty, by pid: a
        # recording that ends between two scans is caught when they exit.
        self._pending: dict[int, dict] = {}
        self.last_bag_event: float | None = None
        self.last_bag_event_iso: str | None = None
        self._w1: int | None = None
        self._w2: int | None = None
        self._wd_dirs: dict[int, Path] = {}
        self._candidate_dirs: set[Path] = set()
        self._next_retry: float | None = None
        self._retry_descriptions_only = False
        self._description_retries = 0
        self._next_heartbeat: float = self.clock() + HEARTBEAT_S
        self._next_foreign_scan: float = self.clock() + FOREIGN_SCAN_INTERVAL_S
        self._harvest_lock = threading.Lock()
        self._harvest_thread: threading.Thread | None = None
        # Bumped on every new recording and when finalise stops waiting for a
        # harvest: a harvest started under an older generation is late, and
        # must not write into a spool that has moved on.
        self._harvest_gen = 0
        # This recording's own harvest result (without bag records), to put
        # back if the spool is cleared while it records.
        self._active_doc: dict | None = None
        self._finalise_deadline: float | None = None
        self._state_lock = threading.Lock()
        self._state_error_at: float | None = None
        self._stop = threading.Event()
        # The watchdog's own (trusted) discovery settings, from watchdog.env.
        # A session.env that omits a key reverts to this baseline rather than
        # leaking the previous session's value (issue #29 review #3).
        self._base_discovery = {k: os.environ.get(k)
                                for k in ros_env.SESSION_ADOPT_KEYS}

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        from inotify_simple import flags
        bags = paths.bags_dir()
        bags.mkdir(parents=True, exist_ok=True, mode=0o775)
        self._w1 = self.ino.add_watch(
            str(bags), flags.CREATE | flags.MOVED_TO)
        self.recover()
        self.write_state()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.start()
        while not self._stop.is_set():
            try:
                self.step(timeout_ms=1000)
            except Exception:
                # One bad iteration (a full disk, a vanished directory) must
                # not kill the service and make systemd restart it in a loop.
                log.exception("watchdog loop error; carrying on")
                time.sleep(1)

    # -- recovery (restart recovery) --------------------------------------

    def recover(self) -> None:
        """Pick up after a restart, a crash or a power cut.

        Finished recordings are finalised straight away. A recording with
        no live recorder and no recent writes was cut off: it is finalised
        too, *without* a new harvest — the context captured before the crash
        is the one that matches it. Only recordings still being written are
        resumed (the first) or queued (the rest).
        """
        previous = read_state() or {}
        harvest_doc, _ = builder.load_spool()
        finalised = {b["path"] for b in (harvest_doc or {}).get("bags", [])}
        try:
            live = {_resolved(Path(r["output_dir"]))
                    for r in self.scan_recorders()
                    if r.get("output_dir") and r.get("active", True)}
        except Exception as exc:
            log.warning("recorder scan failed: %s", exc)
            live = set()

        def still_recording(bag_dir: Path) -> bool:
            mtime = _newest_mtime(bag_dir)
            return _resolved(bag_dir) in live or (
                mtime is not None and time.time() - mtime < BAG_INACTIVITY_S)

        resume: list[Path] = []
        for bag_dir in sorted(p for p in paths.bags_dir().iterdir()
                              if p.is_dir()):
            if str(bag_dir) in finalised:
                continue
            try:
                has_storage = any(_is_storage_file(f.name)
                                  for f in bag_dir.iterdir())
            except OSError:
                continue
            if (bag_dir / "metadata.yaml").is_file():
                self._finalise_offline(bag_dir, "mission_record",
                                       "finished while the assistant was "
                                       "not running")
            elif has_storage and still_recording(bag_dir):
                resume.append(bag_dir)
            elif has_storage:
                self._finalise_offline(bag_dir, "mission_record",
                                       "was cut off (nothing is recording "
                                       "it any more)")
        # Recordings outside the spool tracked before the restart: one that
        # finished (or died) meanwhile is finalised now; one still running is
        # picked up again by the recorder poller.
        for entry in previous.get("tracked_foreign") or []:
            bag_dir = Path(entry)
            if str(bag_dir) in finalised or not bag_dir.is_dir():
                continue
            if (bag_dir / "metadata.yaml").is_file():
                self._finalise_offline(bag_dir, "detected",
                                       "finished while the assistant was "
                                       "not running")
            elif not still_recording(bag_dir):
                self._finalise_offline(bag_dir, "detected",
                                       "was cut off (nothing is recording "
                                       "it any more)")
        for bag_dir in resume:
            if self.state == IDLE:
                log.info("resuming RECORDING for %s after restart", bag_dir)
                self._enter_recording(bag_dir)
            else:
                self._watch_candidate(bag_dir)
                self.queued_bags.append(bag_dir)

    def _finalise_offline(self, bag_dir: Path, source: str, why: str) -> None:
        log.info("recording %s %s; saving what is on disk", bag_dir, why)
        try:
            append_bag_record(bag_dir, source=source)
        except Exception:
            log.exception("failed to finalise %s", bag_dir)

    def _rescan_spool(self) -> None:
        """Re-arm after an inotify queue overflow: events (a new bag folder)
        may have been lost, so look at the spool directly."""
        log.warning("too many file events at once; re-checking the spool")
        harvest_doc, _ = builder.load_spool()
        finalised = {b["path"] for b in (harvest_doc or {}).get("bags", [])}
        for bag_dir in sorted(p for p in paths.bags_dir().iterdir()
                              if p.is_dir()):
            if str(bag_dir) in finalised or \
                    bag_dir in self._wd_dirs.values():
                continue
            self._watch_candidate(bag_dir)
            self._promote_candidate(bag_dir)

    # -- event loop --------------------------------------------------------

    def step(self, timeout_ms: int = 1000) -> None:
        """One loop iteration: drain events, then service timers."""
        for event in self.ino.read(timeout=timeout_ms):
            self._handle_event(event)
        self._service_timers()

    def _handle_event(self, event) -> None:
        from inotify_simple import flags
        mask, name = event.mask, event.name
        if event.wd == -1 or mask & flags.Q_OVERFLOW:
            self._rescan_spool()
            return
        if event.wd == self._w1:
            if mask & flags.ISDIR and mask & (flags.CREATE | flags.MOVED_TO):
                new_dir = paths.bags_dir() / name
                self._watch_candidate(new_dir)
                self._promote_candidate(new_dir)
            return

        bag_dir = self._wd_dirs.get(event.wd)
        if bag_dir is None:
            return
        if _is_storage_file(name) and mask & flags.CREATE:
            if self.state == IDLE and bag_dir in self._candidate_dirs:
                self._enter_recording(bag_dir)
            elif self.state != IDLE and bag_dir != self.active_bag_dir \
                    and bag_dir not in self.queued_bags:
                log.warning("second bag %s appeared while recording %s; "
                            "queued", bag_dir, self.active_bag_dir)
                self.queued_bags.append(bag_dir)
        if bag_dir == self.active_bag_dir and name != "metadata.yaml":
            self._touch_activity()
        if name == "metadata.yaml" and mask & flags.CLOSE_WRITE and \
                bag_dir == self.active_bag_dir and self.state == RECORDING:
            # rosbag2 writes metadata.yaml when it closes the bag. While its
            # recorder process is still alive, let the "recorder done" check
            # finalise once it has exited.
            if not self._recorder_alive(bag_dir):
                self._finalise(bag_dir)

    def _service_timers(self) -> None:
        now = self.clock()
        if now >= self._next_foreign_scan:
            self._next_foreign_scan = now + FOREIGN_SCAN_INTERVAL_S
            self._poll_foreign()
        if self.state == FINALISING:
            self._try_complete_finalise()
        elif self.state == RECORDING:
            # A recorder that has exited and written metadata.yaml is
            # finished now — finalise without waiting out the inactivity window.
            if self._recorder_done(self.active_bag_dir):
                log.info("recorder for %s exited, finalising",
                         self.active_bag_dir)
                self._finalise(self.active_bag_dir)
            elif self.last_bag_event is not None and \
                    now - self.last_bag_event >= BAG_INACTIVITY_S:
                # A quiet bag is not a finished one while its recorder still
                # runs (buffered writes, no traffic on the recorded topics):
                # when we know the pid, wait for its exit.
                if self._recorder_alive(self.active_bag_dir):
                    self._touch_activity()
                else:
                    log.info("bag inactive for %ss, finalising",
                             BAG_INACTIVITY_S)
                    self._finalise(self.active_bag_dir)
            elif self._next_retry is not None and now >= self._next_retry:
                self._maybe_retry_ros()
        if now >= self._next_heartbeat:
            self._next_heartbeat = now + HEARTBEAT_S
            self.write_state()

    # -- recorder detection ------------------------------------------------

    def _spool_bag(self, bag_dir: Path) -> Path | None:
        """The spool path of ``bag_dir`` if it is a spool bag, else None."""
        try:
            if _resolved(bag_dir).parent == _resolved(paths.bags_dir()):
                return paths.bags_dir() / bag_dir.name
        except OSError:
            pass
        return None

    def _is_busy_with(self, bag_dir: Path) -> bool:
        return bag_dir == self.active_bag_dir or bag_dir in self.queued_bags \
            or bag_dir in self._foreign

    @staticmethod
    def _is_finalised(bag_dir: Path) -> bool:
        harvest_doc, _ = builder.load_spool()
        return str(bag_dir) in {
            b.get("path") for b in (harvest_doc or {}).get("bags", [])}

    def _poll_foreign(self) -> None:
        """Track every live recorder found by the /proc scan.

        Recordings outside the spool enter RECORDING when idle (harvest
        adopts the recorder's own DDS env); one found while busy is queued
        like a second spool bag. For spool recordings only the recorder's pid
        is noted: inotify drives those.
        """
        try:
            found = self.scan_recorders()
        except Exception as exc:  # never let a scan glitch kill the loop
            log.warning("recorder scan failed: %s", exc)
            return
        seen_pids = set()
        for rec in found:
            pid = rec.get("pid")
            if rec.get("active", True) is False:
                if pid is not None and pid not in self._pending:
                    self._pending[pid] = dict(rec)
                continue
            seen_pids.add(pid)
            self._pending.pop(pid, None)
            bag_dir = Path(rec["output_dir"])
            spool_bag = self._spool_bag(bag_dir)
            if spool_bag is not None:
                self._recorders[spool_bag] = {"pid": pid,
                                              "start": rec.get("start")}
                continue
            if self._is_busy_with(bag_dir):
                continue
            if self._is_finalised(bag_dir):
                # A live recorder is writing a folder we already finalised:
                # it is being recorded again (or was finalised too early).
                log.warning("%s is being recorded again; its earlier record "
                            "is replaced when this recording ends", bag_dir)
                drop_bag_record(bag_dir)
            self._track_foreign(bag_dir, rec)
        # Recorders seen before they wrote anything that have since exited:
        # a recording shorter than the scan interval.
        for pid, rec in list(self._pending.items()):
            if pid in seen_pids or recorder_scan.pid_alive(pid,
                                                           rec.get("start")):
                continue
            del self._pending[pid]
            bag_dir = recorder_scan.pending_output(rec)
            if bag_dir is None or not (bag_dir / "metadata.yaml").is_file() \
                    or self._spool_bag(bag_dir) is not None \
                    or self._is_busy_with(bag_dir) \
                    or self._is_finalised(bag_dir):
                continue
            log.info("short recording %s finished between checks", bag_dir)
            self._track_foreign(bag_dir, rec)

    def _track_foreign(self, bag_dir: Path, rec: dict) -> None:
        self._foreign[bag_dir] = {"pid": rec.get("pid"),
                                  "start": rec.get("start"),
                                  "discovery": rec.get("discovery", {})}
        if self.state == IDLE:
            log.info("foreign recording detected: %s (pid %s)",
                     bag_dir, rec.get("pid"))
            self._enter_recording(bag_dir)
        else:
            log.warning("foreign recording %s appeared while busy with %s; "
                        "queued", bag_dir, self.active_bag_dir)
            self.queued_bags.append(bag_dir)

    def _recorder_info(self, bag_dir: Path | None) -> dict | None:
        if bag_dir is None:
            return None
        info = self._foreign.get(bag_dir) or self._recorders.get(bag_dir)
        return info if info and info.get("pid") is not None else None

    def _recorder_alive(self, bag_dir: Path | None) -> bool:
        info = self._recorder_info(bag_dir)
        return info is not None and \
            recorder_scan.pid_alive(info["pid"], info.get("start"))

    def _recorder_done(self, bag_dir: Path | None) -> bool:
        """The bag's recorder has closed it: metadata written, and the process
        gone (or still lingering long after writing it)."""
        info = self._recorder_info(bag_dir)
        if info is None or bag_dir is None:
            return False
        meta = bag_dir / "metadata.yaml"
        try:
            meta_age = time.time() - meta.stat().st_mtime
        except OSError:
            return False
        return not self._recorder_alive(bag_dir) or \
            meta_age >= BAG_INACTIVITY_S

    # -- transitions -------------------------------------------------------

    def _watch_candidate(self, bag_dir: Path) -> None:
        from inotify_simple import flags
        try:
            wd = self.ino.add_watch(
                str(bag_dir),
                flags.CREATE | flags.MODIFY | flags.CLOSE_WRITE)
        except OSError as exc:
            log.warning("cannot watch %s: %s", bag_dir, exc)
            return
        self._wd_dirs[wd] = bag_dir
        self._candidate_dirs.add(bag_dir)

    def _promote_candidate(self, bag_dir: Path) -> None:
        """Catch a storage file that already existed when we armed the watch.

        inotify only reports events that happen *after* ``add_watch``, so a
        bag whose first chunk lands in the race window between the directory
        appearing and W2 being armed (or a finished bag dir moved into the
        spool) would otherwise never trigger RECORDING and never be harvested.
        Scan once on arm and apply the same IDLE→enter / busy→queue logic
        the live CREATE event would have.
        """
        try:
            has_storage = any(_is_storage_file(f.name)
                              for f in bag_dir.iterdir())
        except OSError:
            return
        if not has_storage:
            return
        if self.state == IDLE and bag_dir in self._candidate_dirs:
            log.info("storage already present in %s when armed", bag_dir)
            self._enter_recording(bag_dir)
        elif self.state != IDLE and bag_dir != self.active_bag_dir \
                and bag_dir not in self.queued_bags:
            log.warning("second bag %s already had data when seen; queued",
                        bag_dir)
            self.queued_bags.append(bag_dir)

    def _enter_recording(self, bag_dir: Path) -> None:
        if bag_dir not in self._wd_dirs.values():
            self._watch_candidate(bag_dir)
        self._candidate_dirs.discard(bag_dir)
        self.state = RECORDING
        self.since = _now_iso()
        self.active_bag_dir = bag_dir
        self._touch_activity()
        log.info("recording detected: %s", bag_dir)
        self.write_state()
        # This harvest supersedes any retry still pending from an earlier
        # failure; firing both queued a second harvest that could run after
        # the recording (and the robot's stack) had stopped (2026-10-01).
        self._next_retry = None
        self._retry_descriptions_only = False
        self._description_retries = 0
        self._harvest_gen += 1
        self._active_doc = None
        self._run_harvest()

    def _finalise(self, bag_dir: Path | None) -> None:
        """Start finalising ``bag_dir``.

        Waiting for an in-flight harvest no longer blocks the loop: the state
        stays FINALISING (mission_close waits for it) while events, recorder
        scans and heartbeats carry on, and ``_service_timers`` completes it.
        """
        if bag_dir is None or self.state == FINALISING:
            return
        self.state = FINALISING
        self._finalise_deadline = self.clock() + HARVEST_WAIT_S
        thread = self._harvest_thread
        if thread is not None and thread.is_alive():
            # A recording shorter than the harvest pipeline would otherwise
            # be finalised against an empty spool.
            log.info("recording ended before harvest finished; waiting for it")
        self.write_state()
        self._try_complete_finalise()

    def _try_complete_finalise(self) -> None:
        thread = self._harvest_thread
        if thread is not None and thread.is_alive():
            if self._finalise_deadline is not None and \
                    self.clock() < self._finalise_deadline:
                return
            log.warning("harvest still running after %ss; finalising with "
                        "the context on disk", HARVEST_WAIT_S)
            self._harvest_gen += 1  # its result arrives too late to use
        self._complete_finalise()

    def _complete_finalise(self) -> None:
        bag_dir = self.active_bag_dir
        if bag_dir is None:
            return
        if self._active_doc is not None and \
                not paths.harvest_json_path().exists():
            # The spool was cleared while this recording ran (a mission was
            # saved meanwhile): put this recording's own context back.
            log.warning("the spool was cleared during this recording; "
                        "restoring the context captured for it")
            try:
                with _spool_lock():
                    if not paths.harvest_json_path().exists():
                        fsio.atomic_write_json(paths.harvest_json_path(),
                                               self._active_doc)
            except OSError:
                log.exception("restoring the recording's context failed")
        try:
            self._append_bag_record(bag_dir)
        except Exception:
            log.exception("failed to finalise %s", bag_dir)
        self._unwatch(bag_dir)
        self._foreign.pop(bag_dir, None)
        self._recorders.pop(bag_dir, None)
        self.state = IDLE
        self.since = _now_iso()
        self.active_bag_dir = None
        self.last_bag_event = None
        self.last_bag_event_iso = None
        self._finalise_deadline = None
        self._active_doc = None
        log.info("finalised %s", bag_dir)
        self.write_state()
        # A recording that started while we were busy takes over now.
        while self.queued_bags:
            queued = self.queued_bags.pop(0)
            try:
                ready = queued.is_dir() and any(_is_storage_file(f.name)
                                                for f in queued.iterdir())
            except OSError:
                ready = False
            if ready:
                self._enter_recording(queued)
                break
            self._foreign.pop(queued, None)  # gone: stop tracking it

    def _unwatch(self, bag_dir: Path) -> None:
        for wd, known in list(self._wd_dirs.items()):
            if known == bag_dir:
                try:
                    self.ino.rm_watch(wd)
                except OSError:
                    pass
                del self._wd_dirs[wd]

    def _touch_activity(self) -> None:
        self.last_bag_event = self.clock()
        self.last_bag_event_iso = _now_iso()

    # -- harvest -----------------------------------------------------------

    def _run_harvest(self, descriptions_only: bool = False) -> None:
        gen = self._harvest_gen
        if self.harvest_in_thread:
            self._harvest_thread = threading.Thread(
                target=self._harvest_once, args=(gen, descriptions_only),
                daemon=True)
            self._harvest_thread.start()
        else:
            self._harvest_once(gen, descriptions_only)

    def _apply_session_env(self) -> None:
        """Adopt the recording's DDS discovery env for this harvest (issue #29).

        For a ``mission_record`` session this comes from ``<spool>/session.env``;
        for a foreign recording it comes from the recorder's own
        ``/proc/<pid>/environ`` (already filtered to discovery keys by
        ``recorder_scan``). Either way the harvest's ``ros2`` subprocesses and
        rclpy land on the same DDS partition as the session actually recording,
        rather than whatever the possibly-stale ``watchdog.env`` snapshot froze.

        Only :data:`ros_env.SESSION_ADOPT_KEYS` are honoured — both sources are
        untrusted for loader paths (``session.env`` is group-writable; this
        process is root), so paths are never applied. Keys the source does not
        set revert to the watchdog's own baseline so a previous session's value
        never leaks into a later harvest.
        """
        foreign = (self._foreign.get(self.active_bag_dir)
                   if self.active_bag_dir is not None else None)
        if foreign is not None:
            env = dict(foreign.get("discovery", {}))
            label = "recorder process"
        else:
            env = ros_env.safe_session_env(
                ros_env.read_file(paths.session_env_path()))
            label = "recording session"
        for key in ros_env.SESSION_ADOPT_KEYS:
            base = self._base_discovery.get(key)
            if key in env:
                os.environ[key] = env[key]
            elif base is not None:
                os.environ[key] = base
            else:
                os.environ.pop(key, None)
        if env:
            log.info("adopted %s DDS env: %s", label,
                     ", ".join(f"{k}={env[k]}" for k in sorted(env)))

    @staticmethod
    def _spool_mission_id() -> str | None:
        context = builder.load_spool()[1]
        return ((context or {}).get("identity") or {}).get("mission_id")

    def _harvest_once(self, gen: int | None = None,
                      descriptions_only: bool = False) -> None:
        """One harvest. Never raises: a failure is logged and retried."""
        gen = self._harvest_gen if gen is None else gen
        try:
            with self._harvest_lock:
                mission = self._spool_mission_id()
                self._apply_session_env()
                started = time.monotonic()
                doc = self.pipeline()
                status = doc["provenance"]["harvest_status"]
                graph = doc.get("ros_graph") or {}
                log.info("harvest finished in %.0fs: %s; %d nodes, "
                         "parameters for %d", time.monotonic() - started,
                         ", ".join(f"{k}={v}" for k, v in status.items()),
                         len(graph.get("nodes") or []),
                         len(graph.get("parameters") or {}))
                current = gen == self._harvest_gen
                if not current and (
                        not paths.harvest_json_path().exists()
                        or self._spool_mission_id() != mission):
                    log.warning("discarding a harvest that finished too "
                                "late: the recording it was for has been "
                                "closed")
                    return
                if current:
                    self._active_doc = json.loads(json.dumps(doc))
                self._save_harvest(doc, keep_graph=descriptions_only)
                if current:
                    self._schedule_retry(status)
                self.write_state()
        except Exception:
            log.exception("harvest failed; will try again")
            if gen == self._harvest_gen:
                self._retry_descriptions_only = False
                self._next_retry = self.clock() + ROS_RETRY_INTERVAL_S

    def _schedule_retry(self, status: dict) -> None:
        """Retry while ROS is unreachable. A robot that publishes no
        description is retried only a couple of times: re-running the whole
        harvest every minute for it costs a DDS participant each time and
        replaced the recording's graph with ever-later ones."""
        if status.get("ros_graph") in ("failed", "timeout"):
            self._retry_descriptions_only = False
            self._next_retry = self.clock() + ROS_RETRY_INTERVAL_S
        elif status.get("ros_descriptions") in ("failed", "timeout") and \
                self._description_retries < MAX_DESCRIPTION_RETRIES:
            self._description_retries += 1
            self._retry_descriptions_only = True
            self._next_retry = self.clock() + ROS_RETRY_INTERVAL_S
        else:
            self._next_retry = None

    def _maybe_retry_ros(self) -> None:
        """Re-run the full pipeline; cheap modules are cheap, ROS may be up now."""
        thread = self._harvest_thread
        if thread is not None and thread.is_alive():
            self._next_retry = self.clock() + ROS_RETRY_INTERVAL_S
            return
        descriptions_only = self._retry_descriptions_only
        log.info("retrying harvest (%s)",
                 "the robot description wasn't being published"
                 if descriptions_only else "ROS was unreachable")
        self._next_retry = None
        self._run_harvest(descriptions_only=descriptions_only)

    def _save_harvest(self, doc: dict, keep_graph: bool = False) -> None:
        """Write harvest.json, preserving bag records already finalised and
        any ROS capture that was better than this run's."""
        with _spool_lock():
            existing = _load_harvest_for_update()
            if existing:
                doc = _keep_better_ros_capture(
                    existing, doc,
                    prefer_existing=("ros_graph",) if keep_graph else ())
            if existing and existing.get("bags"):
                doc = {**doc, "bags": existing["bags"]}
                if existing.get("provenance", {}).get("harvested_at"):
                    doc["provenance"]["harvested_at"] = \
                        existing["provenance"]["harvested_at"]
            fsio.atomic_write_json(paths.harvest_json_path(), doc)

    def _append_bag_record(self, bag_dir: Path) -> None:
        source = "detected" if bag_dir in self._foreign else "mission_record"
        append_bag_record(bag_dir, source=source)

    # -- state file ----------------------------------------------------------

    def write_state(self) -> None:
        """Write watchdog.state. Never raises: a full disk must not stop the
        watchdog (it used to crash-loop under systemd); the failure is logged
        at most every few minutes."""
        with self._state_lock:
            try:
                harvest_doc, _ = builder.load_spool()
                status = (harvest_doc or {}).get("provenance", {}).get(
                    "harvest_status", {})
                graph = (harvest_doc or {}).get("ros_graph") or {}
                tracked = [self.active_bag_dir, *self.queued_bags]
                fsio.atomic_write_json(paths.watchdog_state_path(), {
                    "version": 1,
                    "pid": os.getpid(),
                    # with the pid, names this process: a later process given
                    # the same pid is not mistaken for the watchdog
                    "proc_start": recorder_scan.proc_start(os.getpid()),
                    "state": self.state,
                    "since": self.since,
                    "heartbeat_at": _now_iso(),
                    "active_bag_dir": str(self.active_bag_dir)
                    if self.active_bag_dir else None,
                    "last_bag_event_at": self.last_bag_event_iso,
                    "harvest_status": status,
                    # when that status was produced, and what it saw — so
                    # readers (doctor) can say "last capture at 10:22", not
                    # imply it's live
                    "harvest_captured_at": graph.get("captured_at"),
                    "harvest_node_count": len(graph.get("nodes") or []),
                    # recordings outside the spool being tracked, so a restart
                    # can finalise those that finish while it is down
                    "tracked_foreign": [str(p) for p in tracked
                                        if p is not None and p in self._foreign],
                })
            except OSError as exc:
                now = time.monotonic()
                if self._state_error_at is None or \
                        now - self._state_error_at >= STATE_ERROR_LOG_S:
                    self._state_error_at = now
                    log.error("can't write the status file (%s); carrying "
                              "on without it", exc)


def _bag_record(bag_dir: Path, source: str, sensors: list) -> dict:
    meta = topic_health.parse_bag_metadata(bag_dir)
    if meta is not None:
        # One read of the message timestamps feeds both the recording window
        # (rosbag2's metadata start/duration can be corrupted by near-epoch
        # messages) and the health analysis.
        series = topic_health.read_clean_series(bag_dir, meta)
        start_s, end_s, duration_s = topic_health.bag_timing(
            bag_dir, meta, series)
        warnings = topic_health.analyse_bag(
            bag_dir, sensors, meta=meta, series=series)
        # duration_s is None when the clock was too unreliable to trust; emit
        # no fabricated times or rates in that case.
        return {
            "path": str(bag_dir),
            "source": source,
            "storage_format": meta["storage_identifier"],
            "size_bytes": fsio.dir_size_bytes(bag_dir),
            "start_time": (datetime.fromtimestamp(
                start_s, tz=timezone.utc).isoformat()
                if start_s is not None else None),
            "end_time": (datetime.fromtimestamp(
                end_s, tz=timezone.utc).isoformat()
                if end_s is not None else None),
            "duration_s": duration_s,
            "message_count": meta["message_count"],
            "topics": [
                {"name": t["name"], "type": t["type"],
                 "message_count": t["message_count"],
                 "avg_frequency_hz": (
                     round(t["message_count"] / duration_s, 3)
                     if duration_s and duration_s > 0 else None)}
                for t in meta["topics"]],
            "health_warnings": warnings,
        }
    # Hard crash mid-write: no metadata.yaml, but the storage itself still
    # names the topics and holds the messages up to the cut — read the
    # timing from it, as for a closed bag.
    cut_off = {"topic": "", "sensor_id": None, "kind": "never_published",
               "start_offset_s": None, "duration_s": None,
               "plain_text": "The recording ended unexpectedly and may be "
                             "incomplete."}
    meta = topic_health.metadata_from_storage(bag_dir)
    series = topic_health.read_clean_series(bag_dir, meta) if meta else None
    start_s = end_s = duration_s = None
    if series:
        start_s, end_s, duration_s = topic_health.bag_timing(
            bag_dir, meta, series)
    if start_s is None and not (series is not None and series.total_read):
        # Nothing readable at all: the files' times are the best guess.
        files = [f for f in bag_dir.rglob("*") if f.is_file()]
        mtimes = [f.stat().st_mtime for f in files] or [time.time()]
        start_s, end_s = min(mtimes), max(mtimes)
        duration_s = end_s - start_s
    warnings = [cut_off] + (topic_health.analyse_bag(
        bag_dir, sensors, meta=meta, series=series) if meta else [])
    topics = (meta or {}).get("topics", [])
    return {
        "path": str(bag_dir),
        "source": source,
        "storage_format": (meta or {}).get("storage_identifier", "unknown"),
        "size_bytes": fsio.dir_size_bytes(bag_dir),
        "start_time": datetime.fromtimestamp(
            start_s, tz=timezone.utc).isoformat()
        if start_s is not None else None,
        "end_time": datetime.fromtimestamp(
            end_s, tz=timezone.utc).isoformat() if end_s is not None else None,
        "duration_s": duration_s,
        "message_count": (meta or {}).get("message_count", 0),
        "topics": [{**t, "avg_frequency_hz": (
            round(t["message_count"] / duration_s, 3)
            if duration_s else None)} for t in topics],
        "health_warnings": warnings,
    }


def append_bag_record(bag_dir: Path, source: str = "mission_record") -> None:
    """Finalise one bag into harvest.json (also used by mission_close to
    salvage bags the watchdog never saw, and by ``ros2 fairy adopt``).

    ``source`` tags how the recording was captured ("mission_record",
    "detected", or "adopted"); foreign sources are referenced in place and
    copied — not moved — into the crate at archive time. An earlier record
    of the same folder is replaced, not duplicated.
    """
    # The bag is read without the lock (it can take a while); only the
    # update of harvest.json is serialised.
    sensors = (builder.load_spool()[0] or {}).get("sensors", [])
    bag = _bag_record(bag_dir, source, sensors)
    with _spool_lock():
        harvest_doc = _load_harvest_for_update()
        if harvest_doc is None:
            harvest_doc = builder.compose_harvest(
                None, None, None, None, None,
                {m: "failed" for m in builder.HARVEST_MODULES})
        harvest_doc["bags"] = [b for b in harvest_doc.get("bags", [])
                               if b.get("path") != str(bag_dir)] + [bag]
        harvest_doc.setdefault("provenance", {})["harvested_at"] = _now_iso()
        fsio.atomic_write_json(paths.harvest_json_path(), harvest_doc)


def drop_bag_record(bag_dir: Path) -> None:
    """Forget the record of ``bag_dir`` (it is being recorded again)."""
    with _spool_lock():
        harvest_doc = _load_harvest_for_update()
        if harvest_doc is None:
            return
        harvest_doc["bags"] = [b for b in harvest_doc.get("bags", [])
                               if b.get("path") != str(bag_dir)]
        fsio.atomic_write_json(paths.harvest_json_path(), harvest_doc)


def refresh_salvaged_records() -> None:
    """Re-read bags recorded without metadata.yaml that now have one.

    A bag finalised before its recorder had closed it (its record says
    ``storage_format: unknown`` or has no topics) is re-read once
    rosbag2 has written its metadata, so the real record is archived.
    """
    harvest_doc, _ = builder.load_spool()
    for bag in (harvest_doc or {}).get("bags", []):
        bag_dir = Path(bag.get("path", ""))
        if (bag.get("storage_format") == "unknown" or not bag.get("topics")) \
                and (bag_dir / "metadata.yaml").is_file():
            log.info("re-reading %s now that it has been closed", bag_dir)
            append_bag_record(bag_dir, source=bag.get("source",
                                                      "mission_record"))


def read_state() -> dict | None:
    """For mission_status: the state file, or None if absent/unreadable."""
    path = paths.watchdog_state_path()
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _writable_dir(path: Path) -> bool:
    """True if `path` exists (or can be created) and this process can write
    into it — a plain presence/permission-bits check isn't enough, since the
    watchdog (usually root) may not own the directory."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".ros_fairy_write_check"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def ensure_ros_log_dir() -> None:
    """Give ROS a logging directory it can actually write to.

    rcl logging expands ~/.ros/log unless ROS_LOG_DIR is set. Under plain
    systemd there is no $HOME at all, but a customized unit or init system
    can still hand the watchdog a $HOME it inherited from somewhere else —
    typically not writable by whatever user the watchdog (usually root)
    actually runs as. Either way, rcl logging then fails to create its log
    dir and node creation silently breaks — which is what broke `ros2 param
    dump` (empty parameters, complete=false) and the rclpy
    /robot_description capture before, while the plain listing commands,
    which never create a node, kept working. Trusting $HOME's mere presence
    reintroduces the same failure the moment $HOME is set-but-unusable, so
    this checks it actually works before relying on it. Applies to this
    process (rclpy) and every harvest subprocess via the inherited
    environment.
    """
    if os.environ.get("ROS_LOG_DIR"):
        return
    home = os.environ.get("HOME")
    if home and _writable_dir(Path(home) / ".ros" / "log"):
        return
    log_dir = paths.ros_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_dir.chmod(0o2775)
    except OSError:
        return  # unwritable (e.g. tests without /var): leave env untouched
    os.environ["ROS_LOG_DIR"] = str(log_dir)


class SpoolLogHandler(logging.Handler):
    """Mirror the watchdog's log into the spool so it is archived with the
    mission (``harvest/watchdog.log``).

    The journal stays on this robot, rotates, and needs privileges to read;
    the crate goes wherever the data goes, so whoever receives a mission can
    see why something wasn't captured. The file is opened per record, so it
    survives the spool being cleared between missions; it is capped so a
    robot left idle for weeks doesn't grow it without bound.
    """

    MAX_BYTES = 1 << 20

    def emit(self, record: logging.LogRecord) -> None:
        try:
            path = paths.watchdog_log_path()
            line = self.format(record) + "\n"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
                size = fh.tell()
            if size > self.MAX_BYTES:  # keep the newest half
                data = path.read_bytes()[-self.MAX_BYTES // 2:]
                cut = data.find(b"\n") + 1
                fsio.atomic_write_text(
                    path, "[older entries dropped]\n"
                    + data[cut:].decode("utf-8", "replace"))
        except Exception:
            self.handleError(record)


_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    spool_log = SpoolLogHandler(level=logging.INFO)
    spool_log.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.getLogger("ros_fairy").addHandler(spool_log)
    safe, dropped = ros_env.root_safe_env(os.environ)
    if dropped:
        # The harvest runs `ros2`, `docker` and `bash` as root: never from a
        # folder an ordinary user can write to (watchdog.env from an older
        # setup froze the operator's PATH, ~/.local/bin included).
        log.warning("ignoring search-path entries any user could change: %s",
                    ", ".join(dropped))
    os.environ.clear()
    os.environ.update(safe)
    ensure_ros_log_dir()
    Watchdog().run()


if __name__ == "__main__":
    main()
