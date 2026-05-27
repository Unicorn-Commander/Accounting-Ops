# Accounting-Ops — test + CI targets.
#
#   make test          unit/smoke tests (no stack needed)
#   make integration   stand up the stack, provision it, run integration tests, tear down
#   make integration-keep   same, but leave the stack running afterwards
#   make integration-up / integration-down   manage the stack by hand
#
# Integration tests require Docker. They SKIP cleanly when the stack is not up,
# so `make test` is always safe on a bare machine.

PY ?= python3
PYTEST ?= $(PY) -m pytest
ENV_SCRIPT := scripts/integration_env.py

# Set WITH_CPA=1 to also build + exercise the cpa-export sibling service.
ifeq ($(WITH_CPA),1)
CPA_FLAG := --with-cpa
else
CPA_FLAG :=
endif

.PHONY: test
test:
	$(PYTEST) tests/smoke -v

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
