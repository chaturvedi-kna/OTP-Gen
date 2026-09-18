"""
Per-instance runtime isolation.

Running two copies of this tool in parallel (the usual setup: one Termux tab
for TemporaSMS, one for VSImpro) used to share every on-disk runtime file:

    stats.json / stats.tmp      - counters of BOTH runs were merged into one
                                  file, so each restart silently inherited the
                                  other run's numbers (and could lose its own);
    state.json / state.tmp      - the recovery record of the last number, so a
                                  crash in tab A overwrote tab B's record;
    .signals/                   - the go/skip file triggers of the manual
                                  trigger flow, shared by both tabs.

An *instance name* namespaces all of them:

    python main.py --provider tempora      -> instance "tempora"
    python main.py --instance tab1         -> instance "tab1"

    stats.json        -> stats.tempora.json
    state.json        -> state.tempora.json
    .signals/         -> .signals-tempora/
    pending_cancels.json -> pending_cancels.tempora.json

The instance name is derived from --provider automatically when that names
exactly one provider, so the two-tab setup needs no extra flag. Pass
--instance explicitly to override (or "" for the legacy shared names).

Everything else that a parallel run may want to own separately (its own
Telegram userbot session, its own Telegram command bot, its own provider
selection) is configured through

    "instances": {
      "tempora": { "meesho_bot": { "session_file": "userbot.tempora.session.txt" },
                   "telegram":   { "bot_token": "...", "chat_id": "..." } }
    }

in config.json: with --instance tempora that block is deep-merged over the top
level config before anything is built, so a tab can run a completely separate
Telegram account / command bot without duplicating the whole file.
"""

import copy
import re

DEFAULT_STATS_FILE = "stats.json"
DEFAULT_STATE_FILE = "state.json"
DEFAULT_PENDING_FILE = "pending_cancels.json"
DEFAULT_SIGNAL_DIR = ".signals"

# An instance name becomes part of a file name, so keep it boring.
_INSTANCE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# --provider values that mean "everything" (no instance can be derived).
_ALL_PROVIDERS = {"all", "both", "", "none", "default"}


def normalize_instance(value):
    """
    Clean up an instance name, or "" for the default (unnamespaced) instance.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = _INSTANCE_RE.sub("-", text).strip("-")
    return text[:32]


def instance_from_provider(provider_value):
    """
    Derive an instance name from a --provider value.

    Only a SINGLE explicit provider gives a stable name: "tempora" -> "tempora".
    Multi-provider values ("tempora,vsimpro"), "all"/"both" and an unset
    provider give "" (the shared default names), because one process then
    speaks for several providers and cannot claim one of their names.
    """
    if provider_value is None:
        return ""
    if isinstance(provider_value, (list, tuple, set)):
        parts = [str(p).strip().lower() for p in provider_value if str(p).strip()]
    else:
        parts = [p.strip().lower() for p in str(provider_value).split(",") if p.strip()]
    parts = [p for p in parts if p not in _ALL_PROVIDERS]
    if len(parts) != 1:
        return ""
    return normalize_instance(parts[0])


def resolve_instance(instance=None, provider=None):
    """--instance wins; otherwise derive it from --provider."""
    explicit = normalize_instance(instance)
    if explicit:
        return explicit
    return instance_from_provider(provider)


def namespaced_name(base, instance):
    """stats.json -> stats.tempora.json (unchanged without an instance)."""
    instance = normalize_instance(instance)
    if not instance:
        return base
    if "." in base:
        stem, _, suffix = base.rpartition(".")
        return f"{stem}.{instance}.{suffix}"
    return f"{base}.{instance}"


def stats_filename(instance):
    return namespaced_name(DEFAULT_STATS_FILE, instance)


def state_filename(instance):
    return namespaced_name(DEFAULT_STATE_FILE, instance)


def pending_filename(instance):
    return namespaced_name(DEFAULT_PENDING_FILE, instance)


def signal_dirname(instance):
    """".signals" -> ".signals-tempora" (the gitignore covers both)."""
    instance = normalize_instance(instance)
    return f"{DEFAULT_SIGNAL_DIR}-{instance}" if instance else DEFAULT_SIGNAL_DIR


def _deep_merge(base, override):
    """Recursively merge `override` into a copy of `base` (dicts only)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def instance_overrides(config, instance):
    """The config["instances"][instance] block, or {} when there is none."""
    if not instance or not isinstance(config, dict):
        return {}
    block = config.get("instances")
    if not isinstance(block, dict):
        return {}
    override = block.get(instance)
    return override if isinstance(override, dict) else {}


def apply_instance_overrides(config, instance):
    """
    Return the config with the instance block merged over it.

    The "instances" key itself never survives into the result, so a nested
    block cannot shadow the top-level settings of a *different* instance.
    """
    if not isinstance(config, dict):
        return config
    # Always a copy: the result is what the run actually uses, and the
    # per-instance blocks are no longer needed once the merge is done.
    merged = _deep_merge(config, instance_overrides(config, instance))
    merged.pop("instances", None)
    return merged
