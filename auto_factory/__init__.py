"""auto_factory: unattended multi-AI pipeline (Step 1 MVP + Step 2 dev bot / merge gateway
+ Step 3 co-supervisor, read-only --check/--status, automated L2 Phase 1 audit).

Sandbox-only coordinator per AGENTS.md section 6. It never touches production
assets (`.env`, `*.sqlite3`, `reports/`) and never merges automatically; every
candidate stops at AWAITING_MERGE (or an exception state) for explicit human
approval bound to `task_id + target_sha + diff_sha256`. The worker never talks to
Telegram; only `dev_bot` does, with its own dedicated credentials
(`AUTO_FACTORY_DEV_BOT_TOKEN` / `AUTO_FACTORY_ADMIN_CHAT_ID`), never product tokens.
"""

__all__ = ["db", "win_job", "worktree", "guardrails", "adapters", "worker", "dev_bot", "supervisor"]
