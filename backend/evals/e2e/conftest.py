# Task snapshots, hidden tests, reviewer patches and cassettes hold test files
# that only the harness runs, against temporary copies. Several fail on
# purpose against the unchanged snapshots, so the backend suite never
# collects them.
collect_ignore_glob = ["tasks/*", "reviewer/*", "cassettes/*"]
