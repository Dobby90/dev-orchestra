"""Host-local capacity protocol, independent of Run snapshot and DB schema.

Order: maintenance -> Run execution -> legacy compatibility gates -> DB slots
-> repository slots -> metadata/resource locks. Never hold a DB transaction while
waiting for a slot, a model, or a test. Slot files are never removed/replaced;
owner records survive crashes until authenticated process cleanup succeeds.
"""

import json
import os
import time
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.process_owner import alive, ensure_stopped, identity

PROTOCOL = 1


def repository_root(repo):
    from dev_orchestra.infrastructure.git import Git

    common = Git(Path(repo)).common().resolve(strict=True)
    stat = common.stat()
    return common, (stat.st_dev, stat.st_ino)


def database_root(path):
    return Path(str(Path(path).resolve()) + ".capacity")


def host_root():
    root = Path("/tmp") / f"devorchestra-resources-{os.getuid()}-v1"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise PipelineError("unsafe host resource coordination directory")
    return root


def policy(root):
    path = root / "policy.json"
    if not path.exists():
        return 1
    try:
        data = json.loads(path.read_text())
        limit = data["limit"]
        if data["protocol"] != PROTOCOL or type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError()
        return limit
    except (ValueError, KeyError, TypeError) as error:
        raise PipelineError("unsupported capacity policy; inspect coordination files") from error


def configure(root, limit):
    if type(limit) is not int or not 1 <= limit <= 32:
        raise PipelineError("capacity must be between 1 and 32")
    with lock(root / "admission.lock", wait=True):
        policy(root)  # Refuse unknown protocols rather than overwriting them.
        write(root / "policy.json", {"protocol": PROTOCOL, "limit": limit})


def configure_capacity(database, total=None, repo=None, repository=None):
    """Explicit shared settings; no per-Run limit can override another DB."""
    if repository is not None and repo is None:
        raise PipelineError("--repo is required with --repository")
    for value in (total, repository):
        if value is not None and (type(value) is not int or not 1 <= value <= 32):
            raise PipelineError("capacity must be between 1 and 32")
    root = None
    if repo is not None:
        common, _ = repository_root(repo)
        root = common / "devorchestra-capacity-v1"
        policy(root)
    policy(database_root(database))
    if total is not None:
        configure(database_root(database), total)
    result = {"total": policy(database_root(database))}
    if root is not None:
        if repository is not None:
            configure(root, repository)
        result["repository"] = policy(root)
    return result


def run_directory(directory):
    """MCP tool scopes live below the Run; retain both process and Run ownership."""
    directory = Path(directory).resolve()
    return next(
        (p for p in (directory, *directory.parents) if (p / "worker.json").is_file()), directory
    )


def _cleanup_owner(path, directory):
    if not path.exists():
        return
    try:
        record = json.loads(path.read_text())
        if record["protocol"] != PROTOCOL:
            raise ValueError()
        previous = Path(record["directory"])
        stat = previous.stat()
        if (
            not previous.is_absolute()
            or not previous.is_dir()
            or previous.is_symlink()
            or record["directory_identity"] != [stat.st_dev, stat.st_ino]
            or not isinstance(record["owner"], dict)
        ):
            raise ValueError()
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise PipelineError("invalid resource owner; manual verification required") from error
    if alive(record["owner"]):
        raise OwnershipError("resource owner is still alive")
    previous_run = Path(record.get("run_directory", str(previous)))
    stat = previous_run.stat()
    if record.get("run_directory_identity", [stat.st_dev, stat.st_ino]) != [
        stat.st_dev,
        stat.st_ino,
    ]:
        raise PipelineError("resource Run directory identity changed")
    current_run = run_directory(directory) if directory is not None else None
    with nullcontext() if previous_run == current_run else lock(previous_run / "execution.lock"):
        ensure_stopped(previous)
        if previous_run != previous:
            ensure_stopped(previous_run)
    path.unlink()


@contextmanager
def _slot(root, directory, stop, limit, waiting):
    directory = Path(directory).resolve()
    held = None
    owner_path = None
    while held is None:
        if action := stop():
            raise StopRequested(action)
        with lock(root / "admission.lock", wait=True, stop=stop):
            capacity = policy(root) if limit is None else limit
            # Count slots above a newly reduced limit too; never terminate their owners.
            try:
                indices = [
                    int(path.stem.removeprefix("slot-")) for path in root.glob("slot-*.lock")
                ]
                if any(index < 0 or index >= 32 for index in indices):
                    raise ValueError()
            except ValueError as error:
                raise PipelineError("invalid capacity slot layout") from error
            size = max(capacity, max((index + 1 for index in indices), default=0))
            free = []
            occupied = 0
            with ExitStack() as probes:
                for index in range(size):
                    handle = ExitStack()
                    try:
                        handle.enter_context(lock(root / f"slot-{index:02d}.lock"))
                    except OwnershipError:
                        handle.close()
                        occupied += 1
                    else:
                        probes.callback(handle.close)
                        free.append((index, handle))
                # Clean *all* abandoned records before admitting a different owner.
                for index, _ in free:
                    _cleanup_owner(root / f"owner-{index:02d}.json", directory)
                if occupied < capacity and free:
                    index, handle = free[0]
                    held = handle.pop_all()
                    owner_path = root / f"owner-{index:02d}.json"
                    try:
                        write(
                            owner_path,
                            {
                                "protocol": PROTOCOL,
                                "owner": identity(os.getpid()),
                                "directory": str(directory),
                                "directory_identity": [
                                    directory.stat().st_dev,
                                    directory.stat().st_ino,
                                ],
                                "run_directory": str(run_directory(directory)),
                                "run_directory_identity": [
                                    run_directory(directory).stat().st_dev,
                                    run_directory(directory).stat().st_ino,
                                ],
                            },
                        )
                    except BaseException:
                        held.close()
                        raise
        if held is None:
            waiting()
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            # A failed cleanup leaves the shared quarantine record for every DB.
            ensure_stopped(directory)
            owner_path.unlink()
        finally:
            held.close()


@contextmanager
def slot(root, directory, stop=lambda: None, limit=None, *, wait_reason="shared_resource"):
    marker = run_directory(directory) / "capacity-wait.json"
    recorded = False

    def waiting():
        nonlocal recorded
        if not recorded:
            write(marker, {"owner": identity(os.getpid()), "reason": wait_reason})
            recorded = True

    try:
        with _slot(root, directory, stop, limit, waiting):
            if recorded:
                marker.unlink(missing_ok=True)
                recorded = False
            yield
    finally:
        if recorded:
            marker.unlink(missing_ok=True)


@contextmanager
def execution_capacity(database, run, stop):
    common, _ = repository_root(run["repo"])
    with (
        # Old workers take these gates exclusively. They cannot overlap new workers.
        lock(Path(database).with_suffix(".worker.lock"), wait=True, stop=stop, shared=True),
        lock(common / "devorchestra.lock", wait=True, stop=stop, shared=True),
        slot(database_root(database), Path(run["directory"]), stop, wait_reason="total_capacity"),
        slot(
            common / "devorchestra-capacity-v1",
            Path(run["directory"]),
            stop,
            wait_reason="repository_capacity",
        ),
    ):
        yield


@contextmanager
def metadata(repo, directory=None):
    from dev_orchestra.infrastructure.process import execution_directory

    directory = directory or execution_directory()
    common, _ = repository_root(repo)
    with (
        lock(common / "devorchestra.lock", wait=True, shared=True),
        lock(common / "devorchestra-metadata-v1.lock", wait=True),
    ):
        # Cleanup/approval callers do not own execution slots. An OS-unlocked
        # repository slot may still protect an orphaned Git child from a dead
        # worker, so mutations must check its durable record as well.
        root = common / "devorchestra-capacity-v1"
        for owner_path in root.glob("owner-*.json"):
            index = owner_path.stem.removeprefix("owner-")
            with ExitStack() as probe:
                try:
                    probe.enter_context(lock(root / f"slot-{index}.lock"))
                except OwnershipError:
                    continue
                _cleanup_owner(owner_path, directory)
        yield


def retire_run_capacity(database, run):
    """Caller owns Run execution.lock and has confirmed Worker/child termination.

    Remove only this Run's abandoned references before its directory is deleted.
    Other owners are neither cleaned nor signalled. Lock files remain permanent.
    """
    common, _ = repository_root(run["repo"])
    directory = Path(run["directory"]).resolve()
    roots = [database_root(database), common / "devorchestra-capacity-v1"]
    roots += [path for path in host_root().iterdir() if path.is_dir()]
    for root in roots:
        if not root.exists():
            continue
        with lock(root / "admission.lock", wait=True):
            for owner_path in root.glob("owner-*.json"):
                record = json.loads(owner_path.read_text())
                if record.get("run_directory", record.get("directory")) != str(directory):
                    continue
                index = owner_path.stem.removeprefix("owner-")
                with lock(root / f"slot-{index}.lock"):
                    _cleanup_owner(owner_path, directory)
