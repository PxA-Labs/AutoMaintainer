"""A minimal in-memory Redis for the indexing lock (SET NX EX and the
compare-and-delete release script)."""


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, key, token):
        # Only the release script is used: delete the key if token owns it.
        assert 'redis.call("del"' in script
        if self.store.get(key) == token:
            del self.store[key]  # ttls keeps the last TTL set, for asserts
            return 1
        return 0
