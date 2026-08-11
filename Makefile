# Accounting-Ops — test + CI targets.
#
#   make test              the whole suite (what CI runs; see the caveat below)
#   make test-fast         stack-free tiers only (smoke + unit)
#   make integration       stand up the stack, provision it, run integration tests, tear down
#   make integration-keep  same, but leave the stack running afterwards
#   make integration-up / integration-down   manage the stack by hand
#   make knowledge-refresh run the Tax Knowledge Curator (requires running app container)
#
# Integration tests require Docker. They SKIP cleanly when the stack is not up,
# so `make test` is safe on a bare machine. It is NOT read-only when a stack IS
# up: the integration tier seeds and mutates the stack's database, so on a
# machine with the stack running, `make test` writes to it. Use `make test-fast`
# for the stack-free tiers only.

PY ?= python3
PYTEST ?= $(PY) -m pytest
ENV_SCRIPT := scripts/integration_env.py

# Set WITH_CPA=1 to also build + exercise the cpa-export sibling service.
ifeq ($(WITH_CPA),1)
CPA_FLAG := --with-cpa
else
CPA_FLAG :=
endif

# Same invocation the CI unit job runs — no explicit path, so `testpaths` in
# pyproject.toml is the single place that decides what "the suite" means and
# local and CI cannot drift apart.
.PHONY: test
test:
	$(PYTEST) -v

# The stack-free tiers only. Identical to what CI's `unit` job runs, and the
# one to reach for when a stack happens to be up and you do not want the
# integration tier writing to it.
test-fast:
	$(PYTEST) -v tests/smoke tests/unit

# ---------------------------------------------------------------------------
# Community Edition self-host
# ---------------------------------------------------------------------------
# One-command bootstrap: generates secrets + admin hash, brings up the
# fleet-free stack (postgres + garage + ollama + app + web), provisions a
# workspace, grants the local admin, and seeds a chart of accounts.
SELFHOST_COMPOSE := docker compose -f docker-compose.selfhost.yml

.PHONY: selfhost
selfhost:
	./scripts/selfhost_bootstrap.sh

.PHONY: selfhost-up
selfhost-up:
	$(SELFHOST_COMPOSE) up -d

.PHONY: selfhost-down
selfhost-down:
	$(SELFHOST_COMPOSE) down

.PHONY: selfhost-logs
selfhost-logs:
	$(SELFHOST_COMPOSE) logs -f --tail=100

# Optional demo seed: two generic sample workspaces (no PII). Runs inside the
# running app container. Requires `make selfhost` (or selfhost-up) first.
.PHONY: demo
demo:
	$(SELFHOST_COMPOSE) exec -T app python scripts/seed_local.py

.PHONY: test-collect
test-collect:
	$(PYTEST) --collect-only -q

.PHONY: integration-up
integration-up:
	$(PY) $(ENV_SCRIPT) up $(CPA_FLAG)

.PHONY: integration-down
integration-down:
	$(PY) $(ENV_SCRIPT) down

.PHONY: integration-test
integration-test:
	@eval "$$($(PY) $(ENV_SCRIPT) env $(CPA_FLAG))"; \
	export ACCTOPS_TEST_BASE_URL ACCTOPS_TEST_DSN ACCTOPS_TEST_APP_DSN ACCTOPS_TEST_TOKEN ACCTOPS_TEST_UC_UID POSTGRES_PASSWORD CPA_EXPORT_URL CPA_EXPORT_TOKEN; \
	$(PYTEST) -m integration tests/integration -v

# Full cycle: up -> test -> down (down always runs, even on test failure).
.PHONY: integration
integration: integration-up
	@set +e; $(MAKE) integration-test; rc=$$?; set -e; \
	$(MAKE) integration-down; \
	exit $$rc

# Same but leaves the stack running for debugging.
.PHONY: integration-keep
integration-keep: integration-up integration-test

# ---------------------------------------------------------------------------
# Tax Knowledge Curator
# ---------------------------------------------------------------------------
# Runs the curator inside the already-running app container.
# The container must be up (`docker compose up -d app`).
#
# ADMIN_DATABASE_URL must be set in the container environment (or .env) so the
# curator can UPDATE (supersede) existing rows — the runtime app role has
# SELECT+INSERT only.  OPENROUTER_API_KEY must also be set for embedding.
#
# Output is written to /drafts/curator-digests/ inside the container and also
# printed to stdout.
#
# Weekly cron (add to /etc/cron.d/accounting-ops on bigboy or midboy1):
#   0 3 * * 0   muut   docker exec accounting_ops-app-1 \
#       python /app/agents/tax_research/knowledge_curator/run_curator.py \
#       --output /drafts/curator-digests/
#
CURATOR_CONTAINER ?= accounting_ops-app-1
CURATOR_OUTPUT    ?= /drafts/curator-digests/

.PHONY: knowledge-refresh
knowledge-refresh:
	@echo "Running Tax Knowledge Curator in container $(CURATOR_CONTAINER)..."
	docker exec $(CURATOR_CONTAINER) \
		python /app/agents/tax_research/knowledge_curator/run_curator.py \
		--output $(CURATOR_OUTPUT)

.PHONY: knowledge-refresh-dry
knowledge-refresh-dry:
	@echo "Dry-run: Tax Knowledge Curator in container $(CURATOR_CONTAINER)..."
	docker exec $(CURATOR_CONTAINER) \
		python /app/agents/tax_research/knowledge_curator/run_curator.py \
		--dry-run
