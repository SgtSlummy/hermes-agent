from pathlib import Path

import yaml


WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "nix-lockfile-fix.yml"
)


def test_nix_lockfile_workflow_scopes_private_key_to_token_generation():
    text = WORKFLOW.read_text(encoding="utf-8")
    payload = yaml.load(text, Loader=yaml.BaseLoader)
    job = payload["jobs"]["auto-fix-main"]

    assert "APP_PRIVATE_KEY" not in job.get("env", {})
    assert text.count("${{ secrets.APP_PRIVATE_KEY }}") == 1

    token_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Generate GitHub App token"
    )
    assert token_step["with"]["private-key"] == "${{ secrets.APP_PRIVATE_KEY }}"
