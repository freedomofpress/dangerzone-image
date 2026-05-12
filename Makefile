GIT_DESC := $(shell git describe --always 2>/dev/null || echo "unknown")

JUNIT_FLAGS := --capture=sys -o junit_logging=all
TEST_GROUP_COUNT ?= 1
TEST_GROUP ?= 1
TEST_GROUP_RANDOM_SEED ?= 999999999
RESULTS_DIR ?= tests/results
LARGE_TEST_REPO_DIR ?= /tmp/dangerzone-test-set

.PHONY: lint ruff ty fix test test-large-init test-large

lint: ruff ty

ruff:
	uv run ruff check

ty:
	uv run ty check

fix:
	uv run ruff check --fix

$(RESULTS_DIR):
	mkdir -p $(RESULTS_DIR)

test: $(RESULTS_DIR)
	TEST_DOCS_DIR="$(TEST_DOCS_DIR)" uv run pytest \
		--test-group-count=$(TEST_GROUP_COUNT) \
		--test-group=$(TEST_GROUP) \
		--test-group-random-seed=$(TEST_GROUP_RANDOM_SEED) \
		--junitxml=$(RESULTS_DIR)/commit_$(GIT_DESC)_$(TEST_GROUP).junit.xml \
		$(JUNIT_FLAGS) \
		$(TEST_FLAGS)

test-large-init:
	git clone --depth 1 https://github.com/freedomofpress/dangerzone-test-set.git $(LARGE_TEST_REPO_DIR)
	cd $(LARGE_TEST_REPO_DIR) && git lfs pull

test-large: TEST_DOCS_DIR = $(LARGE_TEST_REPO_DIR)
test-large: TEST_FLAGS = --tb=no -v
test-large: test-large-init test
	uv run python dev_scripts/large_tests/report.py $(RESULTS_DIR)/commit_$(GIT_DESC)_$(TEST_GROUP).junit.xml
