"""Agent-tracing v1 — wire reconstruction, fingerprinting, and signed manifests.

The reconstructor and fingerprint engines live under ``gateway.pipeline`` so
they can hook into the request path; the higher-level **agent run** aggregator
and signed-manifest builder live here because they live above any single
request.

Public surface:
    :class:`gateway.agent_tracing.manifest.AgentRunManifest`
    :class:`gateway.agent_tracing.aggregator.AgentRunAggregator`
"""
