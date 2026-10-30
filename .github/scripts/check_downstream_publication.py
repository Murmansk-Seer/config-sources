"""Reconcile source updates with verified api-data publication and notification."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import subprocess


def notification_required(source: str, publication: dict | None, receipt: str | None) -> bool:
    if not re.fullmatch(r'[0-9a-f]{40,64}', source):
        raise ValueError('invalid source commit')
    if publication is None:
        return True
    return (
        publication.get('config') != source
        or not re.fullmatch(r'[0-9a-f]{64}', str(publication.get('sha256', '')))
        or not re.fullmatch(r'[0-9a-f]{64}', str(publication.get('fingerprint', '')))
        or not re.fullmatch(r'[0-9a-f]{40,64}', str(publication.get('source_commit', '')))
        or (receipt or '').strip() != publication.get('fingerprint')
    )


def read_remote_file(path: str) -> str | None:
    result = subprocess.run(
        ['gh', 'api', f'repos/Murmansk-Seer/api-data/contents/{path}?ref=main'],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        if 'HTTP 404' in result.stderr:
            return None
        raise RuntimeError(f'api-data publication lookup failed: {result.stderr}')
    content = json.loads(result.stdout)
    return base64.b64decode(content['content'], validate=False).decode('utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--github-output', type=Path, required=True)
    args = parser.parse_args()
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    raw = read_remote_file('.build-state/publication.json')
    publication = json.loads(raw) if raw is not None else None
    if publication is not None and not isinstance(publication, dict):
        raise ValueError('invalid api-data publication record')
    receipt = read_remote_file('.build-state/notified-fingerprint')
    required = notification_required(source, publication, receipt)
    with args.github_output.open('a', encoding='utf-8') as stream:
        stream.write(f'source={source}\nrequired={str(required).lower()}\n')
    print(f'api-data reconciliation: source={source} notification_required={required}')


if __name__ == '__main__':
    main()
