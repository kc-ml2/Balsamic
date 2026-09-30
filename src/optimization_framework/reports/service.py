"""Immutable draft and submission snapshots with exact source validation."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from optimization_framework.storage.sqlite import identifier, now
from .document import digest, sections, utf16_slice


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Annotation(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    section_id: str = Field(min_length=1, max_length=100)
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    exact: str = Field(min_length=1, max_length=60000)
    tier: Literal["good", "poor", "bad", "fine"]
    note: str = Field(default="", max_length=4000)


class Feedback(StrictModel):
    schema_version: Literal[1] = 1
    report_id: str = Field(min_length=1, max_length=100)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    submission_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    expected_submission_id: str | None = Field(default=None, max_length=100)
    annotations: list[Annotation] = Field(default_factory=list, max_length=1000)
    comment: str = Field(default="", max_length=20000)
    focus: str = Field(default="", max_length=4000)


class Reports:
    def __init__(self, store):
        self.store = store

    def get(self, report_id):
        return self.store.get(report_id, "technical_report")

    def add(self, source, title, *, campaign_id=None, parent_id=None, evidence=None, revision=None):
        if len(source.encode()) > 5_000_000 or "<body" not in source.lower():
            raise ValueError("Import a complete HTML report smaller than 5 MB")
        text = sections(source)
        if campaign_id:
            self.store.get(campaign_id, "campaign")
        if parent_id and self.get(parent_id)["campaign_id"] != campaign_id:
            raise ValueError("A revision must belong to its parent's campaign")
        record = {"id": identifier("report"), "title": title, "html": source,
                  "source_hash": digest(source), "sections": text, "campaign_id": campaign_id,
                  "parent_id": parent_id, "created_at": now(), "evidence": evidence or {},
                  "revision": revision}
        return self.store.put_immutable("technical_report", record, "report.created")

    def listing(self, campaign_id=None):
        return [{key: row[key] for key in ("id", "title", "source_hash", "campaign_id", "parent_id", "created_at")}
                | {"url": "/reports/" + row["id"], "latest_submission_id": (self.latest(row["id"]) or {}).get("id")}
                for row in reversed(self.store.list("technical_report", campaign_id))]

    def latest(self, report_id):
        try:
            head = self.store.get("report_review_head_" + report_id, "report_review_head")
            return self.submission(report_id, head["submission_id"])
        except KeyError:
            return None

    def submission(self, report_id, submission_id):
        record = self.store.get("report_feedback_" + submission_id, "report_feedback")
        if record["report_id"] != report_id:
            raise KeyError(submission_id)
        return record

    def submit(self, report_id, feedback):
        value = feedback if isinstance(feedback, Feedback) else Feedback.model_validate(feedback)
        report = self.get(report_id)
        if value.report_id != report_id or value.source_hash != report["source_hash"]:
            raise ValueError("Feedback belongs to a different report version; reopen that exact draft")
        ids, spans, annotations = set(), {}, []
        for item in value.annotations:
            if item.id in ids:
                raise ValueError("Duplicate highlight ID")
            ids.add(item.id)
            if item.section_id not in report["sections"]:
                raise ValueError("Highlight section is missing from the source")
            text = report["sections"][item.section_id]
            if utf16_slice(text, item.start, item.end) != item.exact:
                raise ValueError("Highlight text differs from the source; no feedback was saved")
            for start, end in spans.setdefault(item.section_id, []):
                if item.start < end and start < item.end:
                    raise ValueError("Overlapping highlights must be resolved before submission")
            spans[item.section_id].append((item.start, item.end))
            # The entire source section accompanies every short selection.
            annotations.append(item.model_dump() | {"context": text})
        payload = value.model_dump()
        record = payload | {"id": "report_feedback_" + value.submission_id, "created_at": now(),
                           "campaign_id": report["campaign_id"], "annotations": annotations}
        with self.store.transaction():
            try:
                previous = self.submission(report_id, value.submission_id)
            except KeyError:
                previous = None
            if previous:
                comparable = {key: previous.get(key, "") for key in payload}
                comparable["annotations"] = [{key: item[key] for key in Annotation.model_fields} for item in previous["annotations"]]
                if comparable != payload:
                    raise ValueError("Submission ID already used for different feedback")
                return previous
            latest = self.latest(report_id)
            if (latest or {}).get("submission_id") != value.expected_submission_id:
                raise ValueError("Feedback was submitted in another tab or device. Download your feedback, then reload to inspect the saved review")
            result = self.store.put_immutable("report_feedback", record, "report.feedback_submitted")
            self.store.put("report_review_head", {"id": "report_review_head_" + report_id,
                "campaign_id": report["campaign_id"], "submission_id": value.submission_id})
        self.project(report_id, result)
        return result

    def packet(self, report_id, submission_id):
        from .writer import WRITER_INSTRUCTIONS
        report = self.get(report_id)
        return {"schema_version": 1, "role": "technical_report_writer", "instructions": WRITER_INSTRUCTIONS,
                "report": deepcopy(report), "feedback": self.submission(report_id, submission_id)}

    def project(self, report_id, submission):
        # SQLite is canonical; this convenient handoff can always be regenerated.
        directory = self.store.directory / "reports" / report_id
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (submission["submission_id"] + ".json")
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.packet(report_id, submission["submission_id"]), ensure_ascii=False, indent=2))
            temporary.replace(path)
        except OSError:
            pass  # Download endpoint still serves the committed snapshot.
