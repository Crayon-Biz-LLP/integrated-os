# Test runner convenience targets (plans/75 §12). Single source of truth is
# scripts/run_tests.py — these are one-word aliases, not parallel logic.

.PHONY: test test-fast test-nightly test-all test-app test-app-integration

test: test-fast

test-fast:
	python3 scripts/run_tests.py fast

test-nightly:
	python3 scripts/run_tests.py nightly --live

test-all:
	python3 scripts/run_tests.py all

test-app:
	cd rhodey_app && flutter test

test-app-goldens:
	cd rhodey_app && flutter test --update-goldens test/goldens/

# Requires a booted Android emulator/device (X8, closed v2.13).
test-app-integration:
	cd rhodey_app && flutter test integration_test/ -d $$(flutter devices 2>/dev/null | awk '/emulator-/{print $$1; exit}')
