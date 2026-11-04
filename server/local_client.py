"""Local-package live client factory.

On this machine there is no Muse connector surrogate: the Meta Model
API key is the user's own, supplied through the MODEL_API_KEY
environment variable (see .env.example). bridgelab.muse.MuseClient
already speaks to api.meta.ai with that key and returns the raw API
response dict — the same shape the pipeline expects from the VM's
connector-backed client. This wrapper only adds the attributes the
server and ledger rely on (provenance 'live-muse', model, calls_made).

The key is read by bridgelab.muse from the environment at construction
time. It is never stored on this wrapper, logged, or written to a
ledger (the ledger's credential scrub also applies).
"""


class _LocalEnvClient:
    provenance = "live-muse"

    def __init__(self, timeout=180):
        from bridgelab.muse import MuseClient as _EnvClient

        # Propagate the server's per-call timeout. Previously the wrapper
        # accepted timeout=180 but constructed MuseClient with its default
        # timeout=90, causing later, longer role prompts to time out.
        self._inner = _EnvClient(timeout=timeout)
        self.model = getattr(self._inner, "model", "muse-spark-1.3")
        self.timeout = timeout
        self.calls_made = 0

    def chat(self, messages, tools=None):
        self.calls_made += 1
        return self._inner.chat(messages, tools)


def make_live_client(timeout=180):
    return _LocalEnvClient(timeout=timeout)
