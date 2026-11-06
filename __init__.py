"""Agent-side half of Provider Quota.

The plugin registers no tools, hooks or middleware on purpose: a quota readout
must cost nothing in the model's tool schema. Everything it does lives in the
two UI halves —

  dashboard/plugin_api.py  read-only FastAPI router, mounted at
                           /api/plugins/provider-quota/quota
  desktop/plugin.js        the composer chip, loaded by the Hermes desktop app

`register()` exists so `hermes plugins validate` and the PluginManager see a
normal native plugin. See AGENTS.md before changing either half.
"""


def register(ctx):
    """No agent-side surface by design (see module docstring)."""
