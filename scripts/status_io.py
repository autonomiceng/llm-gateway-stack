"""Bounded private reads and atomic publication for the host status observer."""

import json
import math
import os
import selectors
import signal
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Unavailable(Exception):
    """An observation failed; diagnostics must never become public data."""


class Unsupported(Unavailable):
    """The probe could not execute or yield readable evidence about the component."""


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def run(argv, *, timeout=4, limit=65536, cwd=None, env=None):
    """Drain both pipes with a shared byte/deadline budget, never log their contents."""
    try:
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, cwd=cwd, env=env, start_new_session=True) as process:
            try:
                deadline = time.monotonic() + timeout
                output = bytearray()
                size = 0
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    selector.register(process.stderr, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise Unavailable()
                        for key, _ in selector.select(remaining):
                            chunk = os.read(key.fileobj.fileno(), 8192)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            size += len(chunk)
                            if size > limit:
                                raise Unavailable()
                            if key.fileobj is process.stdout:
                                output.extend(chunk)
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                if code in (125, 126, 127):
                    raise Unsupported()
                if code:
                    raise Unavailable()
                return output.decode('utf-8')
            finally:
                try:
                    # The session/group ID is the child PID captured at spawn.
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
    except (OSError, UnicodeError) as error:
        raise Unsupported() from error
    except subprocess.SubprocessError as error:
        raise Unavailable() from error


def read_json(text, limit=65536):
    if len(text.encode('utf-8')) > limit:
        raise Unavailable()

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Unavailable()
            result[key] = value
        return result

    def finite(text):
        value = float(text)
        if not math.isfinite(value):
            raise Unavailable()
        return value

    try:
        return json.loads(text, object_pairs_hook=unique, parse_float=finite,
                          parse_constant=lambda _: (_ for _ in ()).throw(Unavailable()))
    except (ValueError, RecursionError) as error:
        raise Unavailable() from error


@contextmanager
def directory(path, mode=0o755):
    """Walk with directory descriptors so swapped symlinks cannot redirect writes."""
    if mode & 0o022:
        raise Unavailable()
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    parts = path.parts[1:]
    leaf_created = False
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, mode=mode, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                leaf_created = index == len(parts) - 1
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise Unavailable()
        if leaf_created:
            os.fchmod(fd, mode)
        yield fd
    finally:
        os.close(fd)


def regular(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise Unavailable()


def publish(fd, name, document, mode=0o644):
    payload = (json.dumps(document, separators=(',', ':'), allow_nan=False) + '\n').encode()
    if len(payload) > 65536:
        raise Unavailable()
    regular(fd, name)
    temporary = '.status-' + uuid.uuid4().hex
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode, dir_fd=fd)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        regular(fd, name)
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def task_record(root, env_file, started, state):
    with directory(root / 'data'):
        pass
    with directory(root / 'data' / 'status', 0o700) as fd:
        publish(fd, 'bootstrap.json', {'envFile': str(env_file), 'state': state,
                                      'lastExecutionAt': started}, 0o600)


def read_task(root, env_file):
    with directory(root / 'data' / 'status', 0o700) as fd:
        regular(fd, 'bootstrap.json')
        handle = os.open('bootstrap.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(handle, encoding='utf-8') as stream:
            if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & 0o077:
                raise Unavailable()
            try:
                record = read_json(stream.read(65537))
            except UnicodeError as error:
                raise Unavailable() from error
    if not isinstance(record, dict) or record.get('envFile') != str(env_file):
        raise Unavailable()
    return record
