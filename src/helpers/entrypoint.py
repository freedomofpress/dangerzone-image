#!/usr/bin/python3

import os
import shlex
import subprocess
import sys
import typing

# This script wraps the command-line arguments passed to it to run as an
# unprivileged user in a gVisor sandbox.
# Its behavior can be modified with the following environment variables:
#   RUNSC_DEBUG: If set, print debug messages to stderr, and log all gVisor
#                output to stderr.
#   RUNSC_FLAGS: If set, pass these flags to the `runsc` invocation.
# These environment variables are not passed on to the sandboxed process.


def log(message: str, *values: typing.Any) -> None:
    """Helper function to log messages if RUNSC_DEBUG is set."""
    if os.environ.get("RUNSC_DEBUG"):
        print(message.format(*values), file=sys.stderr)


command = sys.argv[1:]
if len(command) == 0:
    log("Invoked without a command; will execute 'sh'.")
    command = ["sh"]
else:
    log("Invoked with command: {}", " ".join(shlex.quote(s) for s in command))

# Build and write config using runsc bwrap
bwrap_flags = [
    "--unshare-user",
    # Hardcode the UID/GID of the container image to 1000, since we're in
    # control of the image creation, and we don't expect it to change.
    "--uid",
    "1000",
    "--gid",
    "1000",
    "--hostname",
    "dangerzone",
    "--chdir",
    "/",
    "--unshare-pid",
    "--unshare-ipc",
    "--unshare-uts",
    "--ro-bind",
    "/home/dangerzone/dangerzone-image/rootfs",
    "/",
]

mask_dirs = [
    "/boot",
    "/dev",
    # LibreOffice needs a writable home directory, so just mount a tmpfs
    # over it.
    "/home",
    "/media",
    "/mnt",
    "/root",
    "/run",
    "/sbin",
    "/srv",
    "/sys",
    "/tmp",
    "/var",
    # Used for LibreOffice extensions, which are only conditionally
        # installed depending on which file is being converted.
    "/usr/lib/libreoffice/share/extensions/",
]
for d in mask_dirs:
    bwrap_flags += ["--tmpfs", d]

not_forwarded = [
    "PATH",
    "HOME",
    "SHLVL",
    "HOSTNAME",
    "TERM",
    "PWD",
    "RUNSC_FLAGS",
    "RUNSC_DEBUG",
]
for var in not_forwarded:
    bwrap_flags += ["--unsetenv", var]

bwrap_flags += [
    "--setenv",
    "PATH",
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "--setenv",
    "PYTHONPATH",
    "/opt/dangerzone",
    "--setenv",
    "TERM",
    "xterm",
]

if os.environ.get("RUNSC_DEBUG"):
    log("bwrap flags: {}", " ".join(shlex.quote(s) for s in bwrap_flags))

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
    runsc_argv += shlex.split(os.environ.get("RUNSC_FLAGS", ""))

runsc_argv += ["bwrap"] + bwrap_flags + ["--"] + command

log("Running gVisor with command line: {}", " ".join(shlex.quote(s) for s in runsc_argv))
runsc_process = subprocess.run(runsc_argv, check=False)
log("gVisor quit with exit code: {}", runsc_process.returncode)
sys.exit(runsc_process.returncode)
