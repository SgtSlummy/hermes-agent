import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"
USES_PATTERN = re.compile(r"(?m)^[ \t-]*uses:\s*([^\s#]+)")
REVIEWED_NODE24_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/create-github-app-token": "bcd2ba49218906704ab6c1aa796996da409d3eb1",
    "actions/deploy-pages": "cd2ce8fcbc39b97be8ca5fce6e763baed58fa128",
    "actions/github-script": "3a2844b7e9c422d3c10d287c895573f7108da1b3",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/upload-pages-artifact": "fc324d3547104276b827a68afc52ff2a11cc49c9",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "astral-sh/setup-uv": "c771a70e6277c0a99b617c7a806ffedaca235ff9",
    "docker/build-push-action": "53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",
    "docker/login-action": "dbcb813823bdd20940b903addbd779551569679f",
    "docker/setup-buildx-action": "bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
    "google/osv-scanner-action/.github/workflows/osv-scanner-reusable.yml": (
        "9a498708959aeaef5ef730655706c5a1df1edbc2"
    ),
    "marocchino/sticky-pull-request-comment": (
        "5770ad5eb8f42dd2c4f34da00c94c5381e49af88"
    ),
    "oven-sh/setup-bun": "0c5077e51419868618aeaa5fe8019c62421857d6",
    "pypa/gh-action-pypi-publish": "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
    "sigstore/gh-action-sigstore-python": "790bc6befb9d733738f18d8f895854b453640ec9",
}


def _workflow_files() -> list[Path]:
    return sorted((*WORKFLOW_ROOT.glob("*.yml"), *WORKFLOW_ROOT.glob("*.yaml")))


def test_github_workflows_are_valid_yaml():
    for path in _workflow_files():
        payload = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        assert isinstance(payload, dict), path


def test_reviewed_runtime_actions_remain_on_node24_commits():
    seen: set[str] = set()
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        for action_ref in USES_PATTERN.findall(text):
            if action_ref.startswith("./"):
                continue
            action, separator, commit = action_ref.rpartition("@")
            assert separator and re.fullmatch(r"[0-9a-f]{40}", commit), (
                f"{path}: unpinned action reference {action_ref}"
            )
            expected = REVIEWED_NODE24_ACTIONS.get(action)
            if expected is None:
                continue
            seen.add(action)
            assert commit == expected, f"{path}: unreviewed {action} commit"

    assert seen == set(REVIEWED_NODE24_ACTIONS)
