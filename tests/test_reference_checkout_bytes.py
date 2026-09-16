"""Integrity-bound reference bytes survive Windows Git checkout conversion."""
import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
import json

FIXTURE = Path('tests/fixtures/phase-g-reference')


@pytest.mark.parametrize('autocrlf', ['true', 'false'])
@pytest.mark.parametrize('filename,hash_key', [
    ('qualification-reference.json', None),
    ('gain_artifact.json', 'artifact_sha256'),
    ('gain_artifact_input.json', 'artifact_input_sha256')])
def test_reference_checkout_preserves_sidecar_bytes(tmp_path, autocrlf, filename, hash_key):
    if shutil.which('git') is None:
        pytest.skip('Git is required to verify checkout byte preservation')
    rel = FIXTURE / filename
    raw = (ROOT / rel).read_bytes()
    reference = json.loads((ROOT / FIXTURE / 'qualification-reference.json').read_text(encoding='utf-8'))
    expected = (reference['gain'][hash_key] if hash_key else
                (ROOT / rel).with_suffix('.sha256').read_text().split()[0])
    assert hashlib.sha256(raw).hexdigest() == expected
    assert b'\r\n' not in raw
    (tmp_path / '.gitattributes').write_bytes((ROOT / '.gitattributes').read_bytes())
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    def git(*args):
        return subprocess.check_output(['git', '-c', 'safe.directory=' + tmp_path.as_posix(),
                                        '-c', 'core.autocrlf=' + autocrlf, *args], cwd=tmp_path)
    git('init', '-q')
    # Temporary index and objects only; no commit, configuration change or remote.
    git('add', '.gitattributes', rel.as_posix())
    target.unlink()
    git('checkout-index', '--', rel.as_posix())
    assert target.read_bytes() == raw
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected
