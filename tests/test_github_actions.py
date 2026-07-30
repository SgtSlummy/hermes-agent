import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"
USES_PATTERN = re.compile(
    r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})"
)
REVIEWED_NODE24_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "astral-sh/setup-uv": "c771a70e6277c0a99b617c7a806ffedaca235ff9",
    "docker/setup-buildx-action": "bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
}


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_ROOT.glob("*.yml"))


def test_github_workflows_are_valid_yaml():
    for path in _workflow_files():
        payload = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        assert isinstance(payload, dict), path


def test_reviewed_runtime_actions_remain_on_node24_commits():
    seen: set[str] = set()
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        for action, commit in USES_PATTERN.findall(text):
            expected = REVIEWED_NODE24_ACTIONS.get(action)
            if expected is None:
                continue
            seen.add(action)
            assert commit == expected, f"{path}: unreviewed {action} commit"

    assert seen == set(REVIEWED_NODE24_ACTIONS)
