"""GPU owner-memory diagnostic for the MinerU vLLM inference worker.

The package root imports nothing. ``core`` is standard-library only; ``worker``
is loaded solely by vLLM's ``--worker-cls`` resolution inside the engine process.
``reclaim`` exists only in the reclaim-mode image: the API server imports it
through ``--middleware``, and the worker on its first reclaim utility call.
"""
