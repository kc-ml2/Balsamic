"""Durable campaign ownership, Pi assignments, controls, and recovery.

Pi's transcript is reasoning memory. These records remain the authority for
campaign state, grants and scientific outcomes. Network calls never own a SQL
transaction. One workspace service lease serializes all scheduling.
"""
from collections import Counter
from copy import deepcopy
import json
import os
import threading
import time

from optimization_framework.contracts.base import content_hash
from optimization_framework.storage.sqlite import now
from . import usage as llm_usage
from .client import PiClient
from .families import family_of, check_same_family
from . import tiers
from .models import Activate, Configure, Message, Control, ROLES, LEAD

ACTIVE = {"queued", "submitted", "running"}
TERMINAL = {"completed", "failed", "stopped", "interrupted"}
# The harness pushes "agent changed" notices; these sweeps are the safety net
# for a lost notice, a harness restart, or container-side development files.
SWEEP_SECONDS = 60
DEVELOPMENT_SECONDS = 10
STATUS_SECONDS = 60
UNCONFIGURED_STATUS_SECONDS = 10
# Record kinds whose changes require a scheduling pass (dispatch, controls, admission).
SCHEDULING_KINDS = ("agent_run", "agent_session", "agent_campaign", "manager_command", "development_command")
# Models of campaigns activated before per-campaign model choices (OpenAI Codex subscription).
LEGACY_MODELS = {"default": {"provider": "openai-codex", "model": "gpt-6-sol", "effort": "xhigh"},
    "roles": {role: {"provider": "openai-codex", "model": "gpt-6-astra", "effort": "xhigh"}
              for role in (LEAD, "proposal_reviewer", "implementation_validator")}}


def dev_profile():
    """The Pi dev profile directory, or None in locked mode."""
    return os.environ.get("GRATING_PI_PROFILE") or None


def profile_defaults():
    """Dev mode's default model: the profile's settings.json (also Pi's own startup default)."""
    try:
        with open(os.path.join(dev_profile(), "settings.json")) as handle:
            settings = json.load(handle)
    except (TypeError, OSError, ValueError):
        return None
    if not settings.get("defaultProvider") or not settings.get("defaultModel"):
        return None
    return {"provider": settings["defaultProvider"], "model": settings["defaultModel"],
            "effort": settings.get("defaultThinkingLevel")}


class PiController:
    def __init__(self, workspace, client=None):
        self.workspace, self.store = workspace, workspace.store
        self.client = client or PiClient()
        self.threads = {}
        self.dirty = {}
        self.schedule = {}
        self.harness_status = None
        self.schedule_lock = threading.Lock()
        self.diagnostic_processes = {}
        self.tool_locks = {}
        from .development import DevelopmentWorkspaces
        self.development = DevelopmentWorkspaces(self)
        self._migrate_records()
        llm_usage.ensure(self.store)

    def _migrate_records(self):
        """Older records call the lead role "pi" and predate per-campaign models and families."""
        with self.store.transaction():
            agents = self.store.list("agent_session")
            for agent in agents:
                changes = {}
                if agent["role"] == "pi":
                    changes["role"] = LEAD
                if "provider" not in agent:
                    changes["provider"] = "openai-codex"
                    changes["family"] = family_of("openai-codex", agent.get("model"))
                if changes:
                    self.store.put("agent_session", {**agent, **changes})
            for config in self.store.list("agent_campaign"):
                changed = False
                if "pi_id" in config:
                    config["lead_id"] = config.pop("pi_id"); changed = True
                if "llm_family" not in config:
                    config["models"] = deepcopy(LEGACY_MODELS)
                    config["llm_family"] = family_of("openai-codex", LEGACY_MODELS["default"]["model"]); changed = True
                if changed:
                    self.store.put("agent_campaign", config)

    def rollback(self, campaign_id, payload):
        from .models import Rollback
        values = Rollback.model_validate(payload)
        config = self.configuration(campaign_id)
        if not config or config["status"] not in {"paused", "stopped"}:
            raise ValueError("Pause or stop Pi and wait for active turns before rollback")
        if any(r["status"] in ACTIVE for r in self.store.list("agent_run", campaign_id)):
            raise ValueError("Pi turns are still settling; retry after control acknowledgment")
        config.update(enabled=False, rollback_reason=values.reason, rolled_back_at=now())
        self.store.put("agent_campaign", config, "agent.rolled_back")
        return config

    def configuration(self, campaign_id):
        try:
            return self.store.get("pi_campaign_" + campaign_id, "agent_campaign")
        except KeyError:
            return None

    def owns(self, campaign_id):
        return bool((self.configuration(campaign_id) or {}).get("enabled"))

    def activate(self, campaign_id, payload, command_id):
        values = Activate.model_validate(payload)
        existing = self.configuration(campaign_id)
        if existing and existing.get("enabled"):
            return self.view(campaign_id)
        campaign = self.store.get(campaign_id, "campaign")
        if any(r["status"] in {"running", "stopping", "needs_reconciliation"}
               for r in self.store.list("research_run", campaign_id)):
            raise ValueError("Drain or reconcile active legacy model calls before migrating")
        if any(t.get("status") in {"running", "waiting"} for t in self.store.list("discovery_task", campaign_id)):
            raise ValueError("Drain active discovery assignments before migrating")
        sessions = self.store.list("discovery_session", campaign_id)
        manifest = {"id": "pi_migration_" + command_id, "campaign_id": campaign_id, "created_at": now(),
            "legacy_sessions": [{"id": s["id"], "status": s["status"], "policy": s["policy"]} for s in sessions],
            "legacy_autonomy": campaign["autonomy"],
            "budget_snapshot": {k: campaign.get(k) for k in ("compute_budget_seconds", "implementation_compute_budget_seconds",
                "validation_reserve_seconds", "delegated_trial_seconds", "llm_budget_usd")},
            "outstanding_tasks": [t["id"] for t in self.store.list("discovery_task", campaign_id)
                if t["status"] not in {"completed", "cancelled", "superseded"}],
            "hypothesis_ids": [h["id"] for h in self.store.list("hypothesis", campaign_id)],
            "objective": values.objective, "limitations": "Migration preserves unfinished work; no scientific prerequisites are asserted complete."}
        self.store.put_immutable("agent_migration", manifest, "agent.migrated")
        with self.store.connection() as db:
            event_cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
        config = {"id": "pi_campaign_" + campaign_id, "campaign_id": campaign_id, "enabled": True,
            "status": "running", "control_revision": 0, "max_subagents": values.max_subagents,
            "objective": values.objective, "migration_id": manifest["id"], "created_at": now(), "event_cursor": event_cursor,
            "provider": {"configured": False, "reason": "Checking Pi connection"}}
        default = values.model.model_dump() if values.model else profile_defaults() if dev_profile() else None
        config["models"] = {"default": default, "roles": {}} if default else deepcopy(LEGACY_MODELS)
        config["llm_family"] = family_of(config["models"]["default"]["provider"], config["models"]["default"]["model"])
        self.store.put("agent_campaign", config, "agent.activated")
        if values.delegated:
            campaign["autonomy"] = "delegated"
            self.store.put("campaign", campaign, "campaign.delegation_changed")
        for session in sessions:
            if session["status"] not in {"completed", "stopped", "exhausted"}:
                session.update(status="stopped", runtime_successor=config["id"], updated_at=now())
                self.store.put("discovery_session", session, "discovery.migrated")
        for index, hypothesis in enumerate(self.store.list("hypothesis", campaign_id), 1):
            self.store.put_immutable("agent_alias", {"id": "pi_alias_" + hypothesis["id"], "campaign_id": campaign_id,
                "label": f"H{index:02d}", "record_id": hypothesis["id"], "candidate_id": hypothesis.get("candidate_id")})
        lead = self.create_agent(campaign_id, LEAD, values.objective, "lead_" + campaign_id)
        config["lead_id"] = lead["id"]; self.store.put("agent_campaign", config)
        self.enqueue(lead, "pi_boot_" + command_id, "Continue the migrated campaign. Inspect campaign state and the migration manifest, "
            "reuse completed work, reconcile unfinished assignments, and act on the latest unfulfilled researcher request. "
            "Do not repeat conceptual reviews or treat already runnable evaluators as absent.\n" + values.objective)
        return self.view(campaign_id)

    def create_agent(self, campaign_id, role, objective, identity, *, parent_id=None, evidence_ids=None, grant_id=None, output_schema=None):
        if role not in ROLES:
            raise ValueError("Unknown agent role")
        try:
            saved = self.store.get(identity, "agent_session")
            if saved["role"] != role or saved["campaign_id"] != campaign_id or saved.get("parent_agent_id") != parent_id or saved.get("grant_id") != grant_id:
                raise ValueError("Agent identity belongs to a different assignment")
            return saved
        except KeyError:
            pass
        if parent_id:
            parent = self.store.get(parent_id, "agent_session")
            if parent["campaign_id"] != campaign_id or parent["role"] != LEAD:
                raise ValueError("Assignments require this campaign's lead agent")
        config = self.configuration(campaign_id) or {}
        models = config.get("models") or LEGACY_MODELS
        # Saved dev-mode tiers decide a role's model; otherwise the campaign's own choice does.
        choice, tier = self.tier_choice(role)
        choice = choice or models["roles"].get(role) or models["default"]
        # The family lock is per agent: a session never continues on another family, but a
        # campaign may mix families across roles (e.g. a strong lead and a fast specialist).
        family = family_of(choice["provider"], choice["model"])
        record = {"id": identity, "campaign_id": campaign_id, "parent_agent_id": parent_id,
            "role": role, "objective": objective, "evidence_ids": evidence_ids or [], "grant_id": grant_id,
            "provider": choice["provider"], "model": choice["model"], "family": family,
            "tier": tier, "model_source": "tier" if tier else "campaign",
            "reasoning_effort": choice.get("effort"), "status": "queued", "control_revision": 0,
            "created_at": now(), "event_cursor": 0,
            "usage": {"billing_mode": "unknown", "api_cost_usd": 0, "cost_usd": 0, "charged_usd": 0},
            "output_schema": output_schema, "output": None}
        return self.store.put("agent_session", record, "agent.created")

    def _check_available(self, choice):
        """Validate against the harness's last model listing when one is known."""
        models = (self.harness_status or {}).get("models")
        if not models:
            return
        model = next((m for m in models if m["provider"] == choice["provider"] and m["id"] == choice["model"]), None)
        if model is None:
            raise ValueError(f"{choice['provider']}/{choice['model']} is not available from a signed-in provider")
        if choice.get("effort") and choice["effort"] not in model["thinking_levels"]:
            raise ValueError(f"{choice['model']} supports thinking levels {', '.join(model['thinking_levels'])}")

    def tier_choice(self, role):
        """(model, tier id) from saved dev-mode tiers, or (None, None)."""
        if not dev_profile():
            return None, None
        settings = tiers.load(self.store)
        return tiers.choice_for(settings, role) if settings.get("saved") else (None, None)

    def configure(self, campaign_id, payload, command_id):
        values = Configure.model_validate(payload)
        if not dev_profile():
            raise ValueError("Models and thinking levels can be changed only when the Pi harness runs a dev profile")
        config = self.configuration(campaign_id)
        if not config or not config["enabled"]:
            raise ValueError("Activate the agent team for this campaign first")
        if values.follow_tier:
            if values.agent_id is None:
                raise ValueError("Choose the agent that should follow its role's tier")
            agent = self._own_agent(campaign_id, values.agent_id)
            choice, tier = self.tier_choice(agent["role"])
            if choice is None:
                raise ValueError("Save model tiers with a tier for this role first")
            self._switch(agent, choice, command_id, tier=tier, source="tier")
            return self.view(campaign_id)
        choice = values.model.model_dump(exclude={"schema_version"})
        self._check_available(choice)
        if values.agent_id is None:
            # A new campaign default applies to agents created from now on, for every role.
            config["models"] = {"default": choice, "roles": {}, "changed_at": now(), "command_id": command_id}
            self.store.put("agent_campaign", config, "agent.models_changed")
            return self.view(campaign_id)
        agent = self._own_agent(campaign_id, values.agent_id)
        # A model chosen for one agent pins it; tier changes no longer move it.
        self._switch(agent, choice, command_id, tier=None, source="agent")
        return self.view(campaign_id)

    def _own_agent(self, campaign_id, agent_id):
        agent = self.store.get(agent_id, "agent_session")
        if agent["campaign_id"] != campaign_id:
            raise ValueError("Agent belongs to another campaign")
        if agent.get("grant_id"):
            raise ValueError("Implementation agents keep the model frozen with their grant")
        return agent

    def _switch(self, agent, choice, reason, *, tier, source):
        family = check_same_family(agent.get("family") or family_of(agent.get("provider"), agent["model"]),
                                   choice["provider"], choice["model"])
        history = agent.get("model_history", []) + [{"provider": agent.get("provider"), "model": agent["model"],
            "effort": agent.get("reasoning_effort"), "until": now(), "command_id": reason}]
        agent.update(provider=choice["provider"], model=choice["model"], reasoning_effort=choice.get("effort"),
                     family=family, tier=tier, model_source=source, model_history=history[-50:])
        # The harness applies the change at the agent's next turn.
        self.store.put("agent_session", agent, "agent.model_changed")

    def apply_tiers(self, settings):
        """Move tier-following agents to their role's tier; pinned, frozen and cross-family agents stay."""
        updated, blocked = [], []
        reason = f"model_tiers_{settings['revision']}"
        for config in self.store.list("agent_campaign"):
            if not config.get("enabled"):
                continue
            for agent in self.store.list("agent_session", config["campaign_id"]):
                pinned = agent.get("model_source") == "agent" or (not agent.get("model_source") and agent.get("model_history"))
                if agent.get("grant_id") or pinned or agent.get("status") in {"stopped", "completed", "failed"}:
                    continue
                choice, tier = tiers.choice_for(settings, agent["role"])
                if choice is None:
                    continue
                current = (agent.get("provider"), agent["model"], agent.get("reasoning_effort"))
                if current == (choice["provider"], choice["model"], choice.get("effort")) and agent.get("tier") == tier:
                    continue
                try:
                    self._switch(agent, choice, reason, tier=tier, source="tier")
                    updated.append(agent["id"])
                except ValueError as exc:
                    blocked.append({"agent_id": agent["id"], "campaign_id": agent["campaign_id"], "role": agent["role"],
                                    "reason": str(exc)})
        return {"updated": updated, "blocked": blocked}

    def enqueue(self, agent, identity, text, *, mode="follow_up", manager_command_id=None):
        try:
            saved = self.store.get(identity, "agent_run")
            if saved["agent_id"] != agent["id"] or saved["input"] != text or saved["mode"] != mode:
                raise ValueError("Run identity belongs to a different request")
            return saved
        except KeyError:
            pass
        run = {"id": identity, "campaign_id": agent["campaign_id"], "agent_id": agent["id"], "input": text,
            "mode": mode, "status": "queued", "created_at": now(), "manager_command_id": manager_command_id,
            "guidance_revision": self.workspace.memory.state(agent["campaign_id"])["guidance_revision"]}
        self.store.put("agent_run", run, "agent.run_queued")
        if agent["status"] not in {"paused", "stopped"}:
            agent.update(status="queued", updated_at=now()); self.store.put("agent_session", agent)
        return run

    def admit_legacy(self, command):
        config = self.configuration(command["campaign_id"])
        agent = self.store.get(config["lead_id"], "agent_session")
        request = command["request"]
        run = self.enqueue(agent, "pi_run_" + command["id"], request["message"] + "\nResearch request details: " + json.dumps(request)
            + ("\nReview the frozen decision refresh record " + command["decision_refresh_id"] if command.get("decision_refresh_id") else ""), mode="steer",
                           manager_command_id=command["id"])
        command.update(status="dispatched", agent_run_id=run["id"], agent_id=agent["id"])
        self.store.put("manager_command", command, "manager.message_dispatched")
        try:
            message = self.store.get("message_" + command["id"], "message")
            message["agent_id"] = agent["id"]
            self.store.put("message", message)
        except KeyError:
            pass
        for entry in self.store.list("manager_input", command["campaign_id"]):
            if entry.get("manager_command_id") == command["id"] and entry["status"] == "pending":
                entry.update(status="consumed", agent_run_id=run["id"], consumed_at=now())
                self.store.put("manager_input", entry, "manager.input_consumed")
        return run

    def message(self, campaign_id, payload, command_id):
        values = Message.model_validate(payload)
        config = self.configuration(campaign_id)
        if not config or not config["enabled"]:
            raise ValueError("Activate Pi for this campaign first")
        agent = self.store.get(values.agent_id or config["lead_id"], "agent_session")
        if agent["campaign_id"] != campaign_id:
            raise ValueError("Agent belongs to another campaign")
        if config["status"] == "completed":
            config["status"] = "running"
            self.store.put("agent_campaign", config)
        state = self.workspace.memory.state(campaign_id)
        state["guidance_revision"] += 1
        self.store.put("manager_state", state, "manager.guidance_changed")
        if values.question_id:
            question = self.store.get(values.question_id, "agent_question")
            if question["campaign_id"] != campaign_id or question["status"] != "pending":
                raise ValueError("Question is no longer pending in this campaign")
            question.update(status="answered", answer=values.message, answered_at=now())
            self.store.put("agent_question", question, "agent.question_answered")
        self.store.put("message", {"id": "message_" + command_id, "campaign_id": campaign_id, "role": "user",
            "content": values.message, "created_at": now(), "agent_id": agent["id"]}, "message.created")
        return self.enqueue(agent, "pi_run_" + command_id, values.message, mode=values.mode)

    def control(self, campaign_id, payload, command_id):
        values = Control.model_validate(payload)
        config = self.configuration(campaign_id)
        record = self.store.get(values.agent_id, "agent_session") if values.agent_id else config
        if not record or record["campaign_id"] != campaign_id:
            raise ValueError("Control target belongs to another campaign")
        if record["control_revision"] != values.expected_control_revision:
            raise ValueError("Agent control changed; refresh before continuing")
        status = {"pause": "paused", "stop": "stopped", "resume": "running"}[values.action]
        record.update(status=status, control_revision=record["control_revision"] + 1)
        self.store.put("agent_session" if values.agent_id else "agent_campaign", record, "agent.controlled")
        agents = [record] if values.agent_id else self.store.list("agent_session", campaign_id)
        for agent in agents:
            if values.action == "resume" and agent["status"] in {"completed", "stopped"} and agent["role"] != LEAD:
                continue
            agent.update(status=status, pending_control=values.action)
            self.store.put("agent_session", agent)
            for run in self.store.list("agent_run", campaign_id):
                if run["agent_id"] == agent["id"] and run["status"] == "queued" and values.action != "resume":
                    run.update(status=status, undispatched=True)
                    self.store.put("agent_run", run)
            if values.action == "resume":
                undispatched = [r for r in self.store.list("agent_run", campaign_id)
                    if r["agent_id"] == agent["id"] and r["status"] == "paused" and r.get("undispatched")]
                for saved in undispatched:
                    resumed_id = "pi_resume_input_" + content_hash([command_id, saved["id"]])[:28]
                    self.enqueue(agent, resumed_id, saved["input"], mode=saved["mode"])
                    saved.update(status="stopped", superseded_by=resumed_id)
                    self.store.put("agent_run", saved)
                self.enqueue(agent, "pi_resume_" + content_hash([command_id, agent["id"]])[:28],
                    "Resume this assignment from saved session history. Inspect recorded tool receipts and current campaign state before acting; do not duplicate accepted work.")
        return record

    def view(self, campaign_id):
        config = self.configuration(campaign_id)
        return {"configuration": config,
            "agents": [{k: v for k, v in a.items() if k not in {"output_schema", "output"}}
                       for a in self.store.list("agent_session", campaign_id)],
            "questions": [q for q in self.store.list("agent_question", campaign_id) if q["status"] == "pending"],
            "aliases": self.store.list("agent_alias", campaign_id),
            "development": self.development.view(campaign_id)}

    def progress(self, campaign_id):
        view = self.view(campaign_id)
        config, agents = view["configuration"], view["agents"]
        active = any(a["status"] in {"queued", "running"} for a in agents)
        ready = config.get("provider", {}).get("configured", False)
        status = config["status"] if config["status"] in {"paused", "stopped", "completed"} else "running" if active and ready else "waiting"
        return {"status": status, "headline": "Lead agent " + ("is working" if status == "running" else status),
            "message": config.get("provider", {}).get("reason") or ("Connect Pi to OpenAI Codex to continue saved work." if not ready
                else "Persistent Pi sessions coordinate the campaign and its subagents."), "active": status == "running",
            "task_counts": {**Counter(a["status"] for a in agents), "total": len(agents)},
            "agents": [{"task_id": a["id"], "role": a["role"], "stage": "campaign" if a["role"] == LEAD else "assignment",
                "status": a["status"], "model": a["model"], "reasoning_effort": a["reasoning_effort"],
                "activity": a.get("activity", a["objective"]), "last_activity_at": a.get("last_activity_at"),
                "active": a["status"] == "running"} for a in agents]}

    def notify(self, agent_ids):
        """The harness reports agents with new events; inspect only those."""
        campaigns = set()
        for agent_id in agent_ids:
            try:
                agent = self.store.get(agent_id, "agent_session")
            except KeyError:
                continue
            with self.schedule_lock:
                self.dirty.setdefault(agent["campaign_id"], set()).add(agent_id)
            campaigns.add(agent["campaign_id"])
        for campaign_id in campaigns:
            with self.workspace.lock:
                self.tick(campaign_id)
        return {"accepted": len(campaigns)}

    def _stamp(self):
        with self.store.connection() as db:
            event = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        return self.store.revision(*SCHEDULING_KINDS), event

    def _due(self, campaign_id):
        """Whether anything this campaign's agents react to may have changed."""
        state = self.schedule.get(campaign_id)
        clock = time.monotonic()
        with self.schedule_lock:
            dirty = bool(self.dirty.get(campaign_id))
        return (state is None or dirty or state["stamp"] != self._stamp()
                or clock >= state["sweep_at"] or clock >= state["development_at"] or clock >= state["status_at"])

    def tick(self, campaign_id):
        if self.workspace.shutdown_event.is_set() or not self.owns(campaign_id):
            return
        previous = self.threads.get(campaign_id)
        if previous and previous.is_alive():
            return
        if not self._due(campaign_id):
            return
        thread = threading.Thread(target=self._sync, args=(campaign_id,), daemon=True, name="pi-sync-" + campaign_id)
        self.threads[campaign_id] = thread
        self.workspace.research_threads["pi_" + campaign_id] = thread
        thread.start()

    def _sync(self, campaign_id):
        status = None
        clock = time.monotonic()
        # Taken before any work: a change made during this pass is seen next tick.
        stamp = self._stamp()
        state = self.schedule.setdefault(campaign_id, {"sweep_at": 0, "development_at": 0, "status_at": 0, "status": None})
        sweep = clock >= state["sweep_at"]
        failed = False
        with self.schedule_lock:
            dirty = self.dirty.pop(campaign_id, set())
        try:
            if sweep or clock >= state["development_at"]:
                self.development.sync(campaign_id)
                state["development_at"] = clock + DEVELOPMENT_SECONDS
            if sweep or clock >= state["status_at"] or state["status"] is None:
                status = self.client.status()
                if os.environ.get("GRATING_LLM_ENABLED", "true").lower() == "false" or os.environ.get("GRATING_LLM_DISABLED", "false").lower() == "true":
                    status = {**status, "configured": False, "reason": "Model calls are disabled in the server configuration"}
                state["status"] = status
                self.harness_status = status
                state["status_at"] = clock + (STATUS_SECONDS if status.get("configured") else UNCONFIGURED_STATUS_SECONDS)
            status = state["status"]
            with self.workspace.lock, self.store.transaction():
                config = self.configuration(campaign_id)
                if config.get("provider") != status or "sync_error" in config:
                    config["provider"] = status
                    config.pop("sync_error", None)
                    self.store.put("agent_campaign", config)
            for agent in self.store.list("agent_session", campaign_id):
                if not (sweep or agent["id"] in dirty or agent.get("pending_control")
                        or any(run["status"] == "submitted" for run in self.store.list_agent_runs(agent["id"]))):
                    continue
                if agent.get("pending_control"):
                    self.client.control(agent["id"], agent["pending_control"])
                    with self.workspace.lock:
                        current = self.store.get(agent["id"], "agent_session")
                        if current.get("pending_control") == agent["pending_control"]:
                            current.pop("pending_control", None); self.store.put("agent_session", current)
                remote = self.client.inspect(agent["id"], agent.get("event_cursor", 0))
                if remote:
                    self._receive(agent["id"], remote)
                # A POST can fail before reaching an already existing session.
                # Reconcile the exact run, not merely the existence of the agent.
                with self.workspace.lock:
                    for run in self.store.list_agent_runs(agent["id"]):
                        if run["status"] == "submitted" and run["id"] not in (remote or {}).get("runs", {}):
                            current = self.store.get(agent["id"], "agent_session")
                            run["status"] = current["status"] if current["status"] in {"paused", "stopped"} else "queued"
                            run["undispatched"] = True
                            self.store.put("agent_run", run)
            with self.workspace.lock, self.store.transaction():
                config = self.configuration(campaign_id)
                if config["status"] == "completed" and any(c["status"] in {"queued", "waiting_provider", "waiting_discovery"} for c in self.store.list("manager_command", campaign_id)):
                    config["status"] = "running"
                    self.store.put("agent_campaign", config)
                if config["status"] != "running":
                    return
                for command in self.store.list("manager_command", campaign_id):
                    if command["status"] in {"queued", "waiting_provider", "waiting_discovery"}:
                        self.admit_legacy(command)
                self._campaign_events(campaign_id)
                if not status.get("configured"):
                    return
                if not self._within_llm_budget(campaign_id):
                    return
                agents = {a["id"]: a for a in self.store.list("agent_session", campaign_id)}
                runs = self.store.list("agent_run", campaign_id)
                occupied = {r["agent_id"] for r in runs if r["status"] in {"submitted", "running"}}
                children = sum(agents[a]["role"] != LEAD for a in occupied)
                pending = []
                for run in runs:
                    agent = agents[run["agent_id"]]
                    if run["status"] != "queued" or agent["status"] in {"paused", "stopped"}:
                        continue
                    if agent["id"] in occupied and run.get("mode") != "steer":
                        continue
                    if agent["role"] != LEAD and agent["id"] not in occupied and children >= config["max_subagents"]:
                        continue
                    if agent["id"] not in occupied and agent["role"] != LEAD:
                        children += 1
                    occupied.add(agent["id"])
                    run.update(status="submitted", dispatched_at=now()); self.store.put("agent_run", run)
                    pending.append((agent, run))
            for agent, run in pending:
                # A lost POST response is reconciled by exact run ID on the next
                # tick. Never generate a new run ID for a transport retry.
                try:
                    self.client.submit(agent, run)
                except ValueError:
                    if self.client.inspect(agent["id"]) is None:
                        with self.workspace.lock:
                            saved = self.store.get(run["id"], "agent_run")
                            saved["status"] = "queued"; self.store.put("agent_run", saved)
                    raise
        except Exception as exc:
            failed = True
            state["status"] = None
            with self.workspace.lock:
                config = self.configuration(campaign_id)
                if config:
                    # A projection/tool error is not a credential failure. Keep
                    # successful authentication visible and report sync separately.
                    if status is None:
                        config["provider"] = {"configured": False, "reason": str(exc)[:1500]}
                    config["sync_error"] = str(exc)[:1500]
                    self.store.put("agent_campaign", config)
        finally:
            state["stamp"] = stamp
            if sweep or failed:
                # A failed pass retries soon with a full sweep, which covers its dirty agents.
                state["sweep_at"] = clock + (UNCONFIGURED_STATUS_SECONDS if failed else SWEEP_SECONDS)

    def _receive(self, agent_id, remote):
        with self.workspace.lock, self.store.transaction():
            agent = self.store.get(agent_id, "agent_session")
            previous_agent = deepcopy(agent)
            for event in remote.get("events", []):
                self.workspace.agent_log.record(agent["campaign_id"], "pi." + event["type"], agent_id=agent_id,
                    role=agent["role"], parent_agent_id=agent.get("parent_agent_id"),
                    event_key=f"pi:{agent_id}:{event['seq']}", summary=event.get("text", event.get("summary", event["type"]))[:2000], payload=event)
                if event["seq"] > agent.get("event_cursor", 0) and event.get("usage"):
                    usage = agent["usage"]
                    for key in ("input", "output", "cacheRead", "cacheWrite", "reasoning"):
                        usage[key] = usage.get(key, 0) + event["usage"].get(key, 0)
                    usage["calls"] = usage.get("calls", 0) + 1
                    charged = llm_usage.record(self.store, agent["campaign_id"], agent, f"pi:{agent_id}:{event['seq']}", event)
                    usage["cost_usd"] = usage.get("cost_usd", 0) + (event["usage"].get("cost_usd") or 0)
                    usage["charged_usd"] = usage.get("charged_usd", 0) + charged
                    if event.get("billing"):
                        usage["billing_mode"] = event["billing"]
                agent["last_activity_at"] = event["occurred_at"]
                error = event.get("error")
                activity = error if isinstance(error, str) and error else (
                    f"Tool {event.get('tool', 'call')} failed; the agent can inspect its result." if error is True else
                    event.get("text") or event.get("summary") or event["type"])
                agent["activity"] = activity[:500]
            agent.update(event_cursor=remote.get("cursor", agent.get("event_cursor", 0)), pi_session_id=remote.get("session_id"))
            for rid, result in remote.get("runs", {}).items():
                try:
                    run = self.store.get(rid, "agent_run")
                except KeyError:
                    continue
                if run["agent_id"] != agent_id or run["status"] in TERMINAL:
                    continue
                previous = run["status"]
                run.update(status=result["status"], result=result.get("result"), error=result.get("error"))
                self.store.put("agent_run", run, "agent.run_updated" if previous != run["status"] else None)
                if run["status"] in TERMINAL and previous not in TERMINAL:
                    if run.get("manager_command_id"):
                        command = self.store.get(run["manager_command_id"], "manager_command")
                        command.update(status="completed" if run["status"] == "completed" else "handed_off")
                        self.store.put("manager_command", command)
                    text = (run.get("result") or {}).get("text")
                    if text:
                        self.store.put("message", {"id": "message_" + rid, "campaign_id": agent["campaign_id"],
                            "role": "assistant", "origin": "pi", "agent_id": agent_id, "content": text, "created_at": now()}, "message.created")
                    if agent["parent_agent_id"] and (run.get("result") or {}).get("delivery") != "steering_queued":
                        parent = self.store.get(agent["parent_agent_id"], "agent_session")
                        self.enqueue(parent, "pi_child_result_" + content_hash(rid)[:28],
                            f"Subagent {agent_id} ({agent['role']}) finished with status {run['status']}. "
                            f"Read agent run {rid} and its artifacts. Continue the campaign or revise the assignment; a completed response is not proof of scientific completion.")
                    if run["status"] == "interrupted" and not agent.get("grant_id"):
                        self.enqueue(agent, "pi_recover_" + content_hash(rid)[:28],
                            f"Assignment {rid} was interrupted by a harness restart. Read saved receipts and continue from persisted history. Do not repeat committed operations.")
                    if agent["role"] == LEAD and run["status"] == "completed" and (run.get("result") or {}).get("delivery") != "steering_queued":
                        disposition = run.get("disposition", {})
                        if disposition.get("disposition") == "complete":
                            config = self.configuration(agent["campaign_id"])
                            config.update(status="completed", completed_at=now())
                            self.store.put("agent_campaign", config, "agent.objective_completed")
                        elif not disposition:
                            # A response boundary isn't task completion. Continue
                            # with a checkpoint; repeated empty turns ask clearly.
                            stalls = agent.get("uncheckpointed_turns", 0) + 1
                            agent["uncheckpointed_turns"] = stalls
                            if stalls <= 2:
                                self.enqueue(agent, "pi_continue_" + content_hash(rid)[:28],
                                    "Continue the authorized objective using tools. If awaiting work, call yield_work(waiting). If a real blocker needs researcher input, call researcher_ask and yield_work(waiting). Save evidence and use yield_work(complete) only when the objective is achieved.")
                            else:
                                self.store.put("agent_question", {"id": "pi_stalled_" + rid, "campaign_id": agent["campaign_id"],
                                    "agent_id": agent_id, "status": "pending", "created_at": now(),
                                    "question": "The lead agent stopped making tool-backed progress. Give it a revised next step or resume with a narrower assignment.",
                                    "reason": "Three responses ended without a checkpoint; saved work is retained."}, "agent.question_created")
                        else:
                            agent["uncheckpointed_turns"] = 0
            runs = self.store.list_agent_runs(agent_id)
            remaining = [r for r in runs if r["status"] in ACTIVE]
            if agent["status"] not in {"paused", "stopped"}:
                agent["status"] = "running" if remaining else "waiting" if agent["role"] == LEAD else "completed"
                last_run = runs[-1] if runs else None
                if not remaining and last_run and last_run["status"] == "failed":
                    agent.update(status="failed", activity=last_run.get("error") or "Pi turn failed; work is saved. Send a revised direction or resume.")
            if agent != previous_agent:
                self.store.put("agent_session", agent, "agent.progress")

    def _within_llm_budget(self, campaign_id):
        """API-billed agent calls count against the campaign's LLM cap; subscriptions do not."""
        cap = self.store.get(campaign_id, "campaign").get("llm_budget_usd")
        spent = llm_usage.charged(self.store, campaign_id) + self.workspace.implementations.api_committed(campaign_id)
        if cap is None or spent < cap - 1e-9:
            return True
        if any(r["status"] == "queued" for r in self.store.list("agent_run", campaign_id)):
            self.workspace.memory.issue(campaign_id, "llm_budget",
                f"Agent turns are on hold: API spending ${spent:.2f} reached this campaign's LLM cap of ${cap:.2f}. "
                "Raise the cap to continue.", affected="llm_budget_" + campaign_id)
        return False

    def _campaign_events(self, campaign_id):
        config = self.configuration(campaign_id)
        rows = self.store.events(campaign_id, after=config.get("event_cursor", 0), limit=500)
        if not rows:
            return
        relevant = [r for r in rows if r["kind"] in {"trial.evidence_cataloged", "trial.failed", "trial.interrupted",
            "implementation.attached", "implementation.ready", "implementation.evidence_updated",
            "validation.measured", "fixed_mask.completed", "campaign.updated"}]
        for row in rows:
            if row["kind"] == "implementation.updated" and row["data"].get("record_id"):
                grant = self.store.get(row["data"]["record_id"], "implementation_grant")
                if grant["status"] in {"failed", "interrupted", "blocked", "cancelled", "needs_reconciliation"}:
                    relevant.append(row)
            # The lead waits for the researcher's answer to its own requests, such as a trial extension.
            if row["kind"] == "decision.resolved" and row["data"].get("record_id"):
                if self.store.get(row["data"]["record_id"], "decision").get("requested_by") == "lead":
                    relevant.append(row)
        config["event_cursor"] = rows[-1]["id"]; self.store.put("agent_campaign", config)
        if relevant:
            agent = self.store.get(config["lead_id"], "agent_session")
            self.enqueue(agent, "pi_events_" + content_hash([r["id"] for r in relevant])[:28],
                "New campaign outcomes are available. Inspect the records and continue:\n" + json.dumps(relevant))
