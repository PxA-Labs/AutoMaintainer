# Task fixtures contain their own test files (pre-existing and HIDDEN tests)
# that are meant to run only inside the harness, against a temp copy of the
# fixture repository. Keep the backend suite from collecting them: several of
# them fail on purpose against the unchanged fixtures.
collect_ignore_glob = ["tasks/*", "cassettes/*"]
