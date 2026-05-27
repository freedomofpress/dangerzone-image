LARGE_TEST_REPO_DIR ?= tests/test_docs_large

GIT_DESC := $(shell git describe --always 2>/dev/null || echo "unknown")

JUNIT_FLAGS := --capture=sys -o junit_logging=all
TEST_GROUP_COUNT ?= 1
TEST_GROUP ?= 1
TEST_GROUP_RANDOM_SEED ?= 999999999
RESULTS_DIR ?= tests/results

.PHONY: lint ruff ty fix large-tests-list large-tests-requirements large-tests-init large-tests

lint: ruff ty

ruff:
	uv run ruff check

ty:
	uv run ty check

fix:
	uv run ruff check --fix

$(RESULTS_DIR):
	mkdir -p $(RESULTS_DIR)

large-tests-list: large-tests-init
	@echo "=== Test cases in group $(TEST_GROUP) of $(TEST_GROUP_COUNT) ==="
	DZ_RUN_LARGE_TESTS=1 uv run pytest \
		--collect-only \
		--test-group-count=$(TEST_GROUP_COUNT) \
		--test-group=$(TEST_GROUP) \
		--test-group-random-seed=$(TEST_GROUP_RANDOM_SEED) \
		tests/test_large_set.py::TestLargeSet

large-tests-requirements:
	@git-lfs --version || (echo "ERROR: you need to install 'git-lfs'" && false)

large-tests-init: large-tests-requirements
	git clone --depth 1 https://github.com/freedomofpress/dangerzone-test-set.git $(LARGE_TEST_REPO_DIR)
	cd $(LARGE_TEST_REPO_DIR) && git lfs pull

LARGE_TESTS_RESULTS := $(RESULTS_DIR)/commit_$(GIT_DESC)_$(TEST_GROUP).junit.xml

large-tests: large-tests-init $(RESULTS_DIR)
	DZ_RUN_LARGE_TESTS=1 uv run pytest \
		--tb=no \
		--test-group-count=$(TEST_GROUP_COUNT) \
		--test-group=$(TEST_GROUP) \
		--test-group-random-seed=$(TEST_GROUP_RANDOM_SEED) \
		--junitxml=$(LARGE_TESTS_RESULTS) \
		$(JUNIT_FLAGS) \
		-v \
		tests/test_large_set.py::TestLargeSet
	uv run python dev_scripts/large_tests/report.py $(LARGE_TESTS_RESULTS)
