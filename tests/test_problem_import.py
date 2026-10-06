"""Problem examples and the importer: safe uploads, confined agent reads, reviewable drafts."""
import io
import json
import tarfile
import zipfile
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from optimization_framework.api.app import create_app
from optimization_framework.contracts.problem_imports import ProblemExample
from optimization_framework.problem_import.files import ImportError_, ImportFolder, check_git_url, docx_text
from optimization_framework.problem_import.service import ImportCreate, ImportMessage, ProblemImports


class FakePi:
    def __init__(self):
        self.submitted, self.controls, self.runs, self.events = [], [], {}, []

    def status(self):
        return {"configured": True, "mode": "dev", "defaults": {"provider": "deepseek", "model": "deepseek-v4-pro", "effort": None},
                "providers": {"deepseek": {"auth": "api_key", "billing": "api"}},
                "models": [{"provider": "deepseek", "id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro", "thinking_levels": ["off", "high", "max"]}]}

    def submit(self, agent, run):
        self.submitted.append((agent, run))
        self.runs[run["id"]] = {"status": "running"}

    def inspect(self, agent_id, after=0):
        events = [event for event in self.events if event["seq"] > after]
        return {"runs": self.runs, "events": events, "cursor": events[-1]["seq"] if events else after}

    def control(self, agent_id, action):
        self.controls.append((agent_id, action))


def zipped(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def flrl_setup():
    from optimization_framework.evaluation.registry import problems
    example = next(item for item in problems.examples() if item["id"].startswith("flrl"))
    return deepcopy(example["instances"][0])


def draft(**changes):
    return {"title": "FLRL deflector", "summary": "2D dual-polarization deflector.", "objective": "Maximize mean TE/TM +1 efficiency.",
            "instances": [flrl_setup()], "assumptions": ["Normal incidence"], "open_questions": ["Silicon index source"],
            "citations": [{"source": "documents/paper.md.txt", "location": "line 1", "quote": "75 degrees"}], **changes}


def test_installed_examples_offer_both_meent_problems_and_validate(tmp_path):
    with TestClient(create_app(tmp_path, start_workers=False)) as client:
        examples = client.get("/api/v1/problem-examples").json()["examples"]
    ids = {example["id"] for example in examples}
    assert {"meent_grating_1100nm_50deg", "flrl_2d_deflector_1050nm_75deg"} <= ids
    for example in examples:
        for setup in ProblemExample.model_validate(example).instances:
            assert setup.task_input().problem is not None
    flrl = next(example for example in examples if example["id"].startswith("flrl"))
    assert flrl["campaign"]["autonomy"] == "delegated" and "/home/" not in flrl["campaign"]["objective"]


def test_archives_cannot_escape_and_credentials_are_not_copied(tmp_path):
    folder = ImportFolder(tmp_path / "import").create()
    def upload(name, raw):
        path = folder.archive_upload(name)
        path.write_bytes(raw)
        return folder.add_archive(name, path)
    with pytest.raises(ImportError_, match="escapes"):
        upload("bad.zip", zipped({"../evil.py": "x"}))
    summary = upload("code.zip", zipped({"src/model.py": "def score(): pass", ".env": "KEY=1",
        "pkg/.ssh/id_rsa": "secret", "node_modules/x.js": "x", "data/big.bin": b"\0" * (6 * 1024**2)}))
    # Large files are kept and flagged; the agent decides whether they matter.
    assert summary["files"] == 2 and summary["large_files"] == 1 and (folder.code / "data/big.bin").stat().st_size == 6 * 1024**2
    assert {row["path"] for row in summary["skipped"]} == {".env", "pkg/.ssh/id_rsa", "node_modules/x.js"}
    listed = {row["path"]: row for row in folder.listing("code", depth=3)["entries"]}
    assert listed["code/data/big.bin"] == {"path": "code/data/big.bin", "type": "file", "bytes": 6 * 1024**2, "large": True, "binary": True}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        link = tarfile.TarInfo("link"); link.type = tarfile.SYMTYPE; link.linkname = "/etc/passwd"
        archive.addfile(link)
        data = b"print(1)"; info = tarfile.TarInfo("run.py"); info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    summary = upload("code.tar.gz", buffer.getvalue())
    assert summary["files"] == 1 and not (folder.code / "link").exists()


def test_local_folders_exclude_account_settings_and_secrets(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    project = home / "Work" / "flrl"
    (project / ".git").mkdir(parents=True)
    (project / "train.py").write_text("print('train')")
    (project / "secrets.json").write_text("{}")
    (project / ".git" / "config").write_text("[core]")
    folder = ImportFolder(tmp_path / "import").create()
    for path, message in ((home / ".ssh", "credentials"), (home, "home folder"), (tmp_path / "elsewhere", "does not exist")):
        with pytest.raises(ImportError_, match=message):
            folder.add_folder(str(path), home=home)
    summary = folder.add_folder(str(project), home=home)
    assert summary["files"] == 1 and (folder.code / "train.py").exists() and not (folder.code / ".git").exists()


def test_git_sources_are_limited_to_network_urls():
    check_git_url("https://github.com/kc-ml2/FLRL.git")
    check_git_url("git@github.com:kc-ml2/FLRL.git")
    for url in ("file:///etc", "/home/chs/repo", "ext::sh -c touch", "-uhttps://x/y", "http://example.com/repo"):
        with pytest.raises(ImportError_):
            check_git_url(url)


def test_document_text_keeps_locations_and_agent_reads_stay_inside(tmp_path):
    body = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>Period 525 nm</w:t></w:r></w:p><w:p><w:r><w:t>TE and TM</w:t></w:r></w:p></w:body></w:document>')
    assert docx_text("s.docx", zipped({"word/document.xml": body})) == "[para 1] Period 525 nm\n[para 2] TE and TM"
    folder = ImportFolder(tmp_path / "import").create()
    folder.add_document("paper.md", b"# Deflector\nThe target angle is 75 degrees.\n")
    assert folder.read("documents/paper.md.txt")["text"].splitlines()[1] == "2: The target angle is 75 degrees."
    assert folder.search("75 degrees")["matches"][0]["path"] == "documents/paper.md.txt"
    (folder.code / "data.csv").write_text("x\n" * 1_500_000 + "needle\n")
    found = folder.search("needle")
    assert found["matches"] == [] and found["skipped_large_files"][0]["path"] == "code/data.csv"
    assert folder.search("needle", "code/data.csv")["matches"][0]["line"] == 1_500_001
    page = folder.read("code/data.csv", start_line=1_499_999, max_lines=5)
    assert page["text"].splitlines() == ["1499999: x", "1500000: x", "1500001: needle"] and not page["more"]
    for path in ("../uploads/document-paper.md", "/etc/passwd", "documents/../../uploads"):
        with pytest.raises(ValueError):
            folder.read(path)
    with pytest.raises(ImportError_, match="supported documents"):
        folder.add_document("tool.exe", b"MZ")


def test_import_produces_a_reviewable_draft_through_the_harness(tmp_path):
    from optimization_framework.execution.service import Workspace
    workspace, pi = Workspace(tmp_path), FakePi()
    imports = ProblemImports(workspace, client=pi)
    with pytest.raises(ValueError, match="not available"):
        imports.create(ImportCreate(provider="deepseek", model="unknown"))
    record = imports.create(ImportCreate(provider="deepseek", model="deepseek-v4-pro", effort="high", notes="Use the 2D condition"))
    with pytest.raises(ValueError, match="at least one document"):
        imports.start(record["id"])
    imports.add_document(record["id"], "paper.md", b"The target angle is 75 degrees.")
    record = imports.start(record["id"])
    agent, run = pi.submitted[0]
    assert agent["role"] == "problem_importer" and agent["id"] == record["id"] and "documents/paper.md.txt" in run["input"]
    with pytest.raises(ValueError, match="only before the import starts"):
        imports.add_document(record["id"], "late.md", b"x")
    manifest = imports.manifest(record["id"], run["id"])
    assert {tool["name"] for tool in manifest["tools"]} == {"files_list", "file_read", "files_search", "problem_catalog", "formulation_submit"}
    catalog = imports.call(record["id"], run["id"], "c1", "problem_catalog", {})
    assert "meent_2d_dual_polarization_deflector" in {adapter["problem_id"] for adapter in catalog["adapters"]}
    assert "error" in imports.call(record["id"], run["id"], "c2", "file_read", {"path": "../uploads/document-paper.md"})
    broken = draft(instances=[{**flrl_setup(), "configuration": {"wavelength_nm": 1050}}])
    assert "error" in imports.call(record["id"], run["id"], "c3", "formulation_submit", {"draft": broken})
    assert imports.call(record["id"], run["id"], "c4", "formulation_submit", {"draft": draft()})["accepted"]
    pi.events = [{"seq": 1, "type": "assistant.message", "text": "Formulated the FLRL deflector.", "billing": "api",
                  "usage": {"input": 1000, "output": 200, "cacheRead": 0, "reasoning": 50, "cost_usd": 0.01}}]
    pi.runs[run["id"]] = {"status": "completed", "result": {"text": "Formulated; one open question."}}
    record = imports.sync(record["id"])
    assert record["status"] == "ready" and record["reply"] == "Formulated; one open question."
    assert record["usage"]["calls"] == 1 and record["usage"]["charged_usd"] == 0.01
    assert any("Read ../uploads" in row["text"] for row in record["activity"])
    saved = imports.save_example(record["id"])
    assert saved["source"] == "saved" and saved["id"] in {row["id"] for row in imports.examples()}
    imports.message(record["id"], ImportMessage(message="Use the measured silicon index"))
    assert pi.submitted[-1][1]["input"].startswith("Researcher request:") and imports.get(record["id"])["status"] == "running"


def test_api_billed_import_stops_at_its_budget(tmp_path):
    from optimization_framework.execution.service import Workspace
    workspace, pi = Workspace(tmp_path), FakePi()
    imports = ProblemImports(workspace, client=pi)
    record = imports.create(ImportCreate(provider="deepseek", model="deepseek-v4-pro", budget_usd=0.05))
    imports.add_document(record["id"], "paper.md", b"text")
    imports.start(record["id"])
    pi.events = [{"seq": 1, "type": "assistant.message", "billing": "api", "usage": {"cost_usd": 0.06}}]
    record = imports.sync(record["id"])
    assert record["status"] == "stopped" and "budget" in record["error"] and pi.controls == [(record["id"], "stop")]


def test_harness_callbacks_for_importers_reach_the_importer(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("secret-token")
    monkeypatch.setenv("GRATING_PI_TOKEN_FILE", str(token))
    app = create_app(tmp_path / "workspace", start_workers=False)
    with TestClient(app) as client:
        imports = app.state.workspace.problem_imports
        imports._client = FakePi()
        record = client.post("/api/v1/problem-imports", json={"provider": "deepseek", "model": "deepseek-v4-pro"}).json()
        uploaded = client.put(f"/api/v1/problem-imports/{record['id']}/documents/paper.md", content=b"Angle 75 degrees")
        assert uploaded.status_code == 200 and uploaded.json()["documents"][0]["text_file"] == "documents/paper.md.txt"
        run_id = client.post(f"/api/v1/problem-imports/{record['id']}/start").json()["runs"][0]
        headers = {"Authorization": "Bearer secret-token"}
        manifest = client.post("/api/internal/pi/manifest", json={"agent_id": record["id"], "run_id": run_id}, headers=headers).json()
        assert "formulation_submit" in {tool["name"] for tool in manifest["tools"]}
        found = client.post("/api/internal/pi/tool", headers=headers, json={"agent_id": record["id"], "run_id": run_id,
            "call_id": "c1", "name": "files_search", "arguments": {"pattern": "75"}}).json()
        assert found["matches"][0]["line"] == 1
        assert client.post("/api/internal/pi/tool", json={"agent_id": record["id"], "run_id": run_id,
            "call_id": "c2", "name": "files_list", "arguments": {}}).status_code == 401


def test_importer_uses_the_tier_assigned_to_its_role(tmp_path):
    from optimization_framework.agents import tiers
    from optimization_framework.execution.service import Workspace
    workspace, pi = Workspace(tmp_path), FakePi()
    tiers.save(workspace.store, tiers.TierSettings.model_validate({"tiers": [
        {"id": "strong", "label": "Strong", "model": {"provider": "deepseek", "model": "deepseek-v4-pro", "effort": "max"}},
        {"id": "fast", "label": "Fast", "model": {"provider": "deepseek", "model": "deepseek-v4-pro", "effort": "off"}}],
        "roles": {"problem_importer": "strong"}}))
    imports = ProblemImports(workspace, client=pi)
    record = imports.create(ImportCreate())
    assert (record["tier"], record["model"]["effort"]) == ("strong", "max")
    assert imports.create(ImportCreate(tier="fast"))["model"]["effort"] == "off"
    with pytest.raises(ValueError, match="model tier"):
        imports.create(ImportCreate(tier="missing"))
