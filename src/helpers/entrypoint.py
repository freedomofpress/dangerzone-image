#!/usr/bin/python3
"""gVisor sandbox entrypoint.

Builds an OCI runtime config and execs runsc. The config build is factored
into ``build_oci_config()`` so it can be tested independently of the runtime.

Environment variables that control this entrypoint (consumed here, never
forwarded into the sandbox):

    RUNSC_DEBUG  If set, print debug messages to stderr and log all gVisor
                 output to stderr.
    RUNSC_FLAGS  If set, pass these flags to the ``runsc`` invocation.
"""

import copy
import json
import os
import shlex
import subprocess
import sys
import typing

# Env vars forwarded from the outer container into the sandbox. Any var not
# listed here is dropped at the sandbox boundary -- including sensitive state
# the outer container may have inherited (cloud credentials, CI tokens, API
# keys, and so on). Dropping (rather than masking with ``<ENV>=``) means the
# name itself never crosses the boundary.
ALLOWED_ENV: typing.FrozenSet[str] = frozenset(
    {"LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE", "TZ"}
)

# Baseline env vars the sandboxed process always receives. Set by the
# entrypoint itself; not inherited from the parent environment.
_BASELINE_ENV: typing.Tuple[str, ...] = (
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "PYTHONPATH=/opt/dangerzone",
    "TERM=xterm",
)

_BASE_OCI_CONFIG: dict[str, typing.Any] = {
    "ociVersion": "1.0.0",
    "process": {
        "user": {
            # Hardcode the UID/GID of the container image to 1000, since we're in
            # control of the image creation, and we don't expect it to change.
            "uid": 1000,
            "gid": 1000,
        },
        "args": [],
        "env": [],
        "cwd": "/",
        "capabilities": {
            "bounding": [],
            "effective": [],
            "inheritable": [],
            "permitted": [],
        },
        "rlimits": [
            {"type": "RLIMIT_NOFILE", "hard": 4096, "soft": 4096},
        ],
    },
    "root": {"path": "rootfs", "readonly": True},
    "hostname": "dangerzone",
    "mounts": [
        # Mask almost every system directory of the outer container, by mounting tmpfs
        # on top of them. This is done to avoid leaking any sensitive information,
        # either mounted by Podman/Docker, or when gVisor runs, since we reuse the same
        # rootfs. We basically mask everything except for `/usr`, `/bin`, `/lib`,
        # `/etc`, and `/opt`.
        #
        # Note that we set `--root /home/dangerzone/.containers` for the directory where
        # gVisor will create files at runtime, which means that in principle, we are
        # covered by the masking of `/home/dangerzone` that follows below.
        #
        # Finally, note that the following list has been taken from the dirs in our
        # container image, and double-checked against the top-level dirs listed in the
        # Filesystem Hierarchy Standard (FHS) [1]. It would be nice to have an allowlist
        # approach instead of a denylist, but FHS is such an old standard that we don't
        # expect any new top-level dirs to pop up any time soon.
        #
        # [1] https://en.wikipedia.org/wiki/Filesystem_Hierarchy_Standard
        {
            "destination": "/boot",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        {
            "destination": "/dev",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev"],
        },
        {
            "destination": "/home",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        {
            "destination": "/media",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        {
            "destination": "/mnt",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        {
            "destination": "/proc",
            "type": "proc",
            "source": "proc",
        },
        {
            "destination": "/root",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        {
            "destination": "/run",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev"],
        },
        {
            "destination": "/sbin",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        {
            "destination": "/srv",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        {
            "destination": "/sys",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        {
            "destination": "/tmp",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev"],
        },
        {
            "destination": "/var",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev"],
        },
        # LibreOffice needs a writable home directory, so just mount a tmpfs
        # over it.
        {
            "destination": "/home/dangerzone",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev"],
        },
        # Used for LibreOffice extensions, which are only conditionally
        # installed depending on which file is being converted.
        {
            "destination": "/usr/lib/libreoffice/share/extensions/",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "noexec", "nodev"],
        },
    ],
    "linux": {
        "namespaces": [
            {"type": "pid"},
            {"type": "network"},
            {"type": "ipc"},
            {"type": "uts"},
            {"type": "mount"},
        ],
    },
}


def build_oci_config(
    command: typing.Sequence[str],
    env: typing.Mapping[str, str],
) -> dict[str, typing.Any]:
    """Build an OCI runtime config for running ``command`` in the sandbox.

    Only env vars in :data:`ALLOWED_ENV` are forwarded from ``env``. Every
    other var in ``env`` is dropped -- its name is not written into the
    config at all.

    The baseline vars in :data:`_BASELINE_ENV` are always set, and cannot be
    overridden by ``env``.
    """
    config = copy.deepcopy(_BASE_OCI_CONFIG)
    config["process"]["args"] = list(command)
    config["process"]["env"] = list(_BASELINE_ENV) + [
        f"{key}={val}" for key, val in env.items() if key in ALLOWED_ENV
    ]
    return config


def log(message: str, *values: typing.Any) -> None:
    """Helper function to log messages if RUNSC_DEBUG is set."""
    if os.environ.get("RUNSC_DEBUG"):
        print(message.format(*values), file=sys.stderr)


def main() -> int:
    command = sys.argv[1:]
    if len(command) == 0:
        log("Invoked without a command; will execute 'sh'.")
        command = ["sh"]
    else:
        log("Invoked with command: {}", " ".join(shlex.quote(s) for s in command))

    oci_config = build_oci_config(command, os.environ)

    if os.environ.get("RUNSC_DEBUG"):
        log("Command inside gVisor sandbox: {}", command)
        log("OCI config:")
        json.dump(oci_config, sys.stderr, indent=2, sort_keys=True)
        # json.dump doesn't print a trailing newline, so print one here:
        log("")
    with open("/home/dangerzone/dangerzone-image/config.json", "w") as oci_config_out:
        json.dump(oci_config, oci_config_out, indent=2, sort_keys=True)

    # Run gVisor.
    runsc_argv = [
        "/usr/bin/runsc",
        "--rootless=true",
        "--network=none",
        "--root=/home/dangerzone/.containers",
        # Disable DirectFS for to make the seccomp filter even stricter,
        # at some performance cost.
        "--directfs=false",
    ]
    if os.environ.get("RUNSC_DEBUG"):
        runsc_argv += ["--debug=true", "--alsologtostderr=true"]
    if os.environ.get("RUNSC_FLAGS"):
        runsc_argv += [x for x in shlex.split(os.environ.get("RUNSC_FLAGS", "")) if x]
    runsc_argv += ["run", "--bundle=/home/dangerzone/dangerzone-image", "dangerzone"]
    log(
        "Running gVisor with command line: {}",
        " ".join(shlex.quote(s) for s in runsc_argv),
    )
    runsc_process = subprocess.run(runsc_argv, check=False)
    log("gVisor quit with exit code: {}", runsc_process.returncode)
    return runsc_process.returncode


if __name__ == "__main__":
    sys.exit(main())
