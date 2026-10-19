#!/usr/bin/env python3
"""Refresh the relay's CO administrator policy from SRC-managed Unix groups."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def group_members(group, managed_file='/etc/rsc/managedgroups'):
    try:
        if group not in Path(managed_file).read_text().splitlines():
            return []
        result = subprocess.run(['getent', 'group', group], capture_output=True, text=True,
                                check=True, timeout=10)
        fields = result.stdout.strip().split(':')
        if len(fields) != 4 or fields[0] != group:
            return []
        return sorted({user for user in fields[3].split(',') if user})
    except (OSError, subprocess.SubprocessError):
        return []


def write_policy(directory, members):
    directory = Path(directory)
    directory.mkdir(mode=0o755, exist_ok=True)
    path = directory / 'admins.json'
    temporary = directory / 'admins.json.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, 'w') as output:
        json.dump({'members': members, 'expires_at': time.time() + 180}, output)
    os.chmod(temporary, 0o644)
    temporary.replace(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', default='src_co_admin')
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    write_policy(args.directory, group_members(args.group))
