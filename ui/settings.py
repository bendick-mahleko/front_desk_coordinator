"""The settings view.

What this process is actually running with — which model answers, which model
screens, which embeddings built the index, and the clinic policy knobs that are
data rather than code.

It exists because "which model is this?" is the first question anyone asks of a
demo, and reading it off a config file is a poor answer when the running process
may have been started with different environment variables.

No secret appears here. The credential is shown by *source*, never by value.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

PROVIDER_LABEL = {
    "anthropic": "Anthropic (first party)",
    "openrouter": "OpenRouter",
}

CREDENTIAL_LABEL = {
    "ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY (environment)",
    "ANTHROPIC_AUTH_TOKEN": "ANTHROPIC_AUTH_TOKEN (environment)",
    "OPENROUTER_API_KEY": "OPENROUTER_API_KEY (environment)",
    "ant profile": "stored profile (ant auth login)",
    "none": "none found",
}


def render(config: dict[str, Any] | None) -> None:
    if not config:
        st.caption("Settings are unavailable — the API did not respond.")
        return

    model = config.get("language_model", {})
    knowledge = config.get("knowledge_base", {})
    policy = config.get("clinic_policy", {})
    service = config.get("service", {})
    storage = config.get("storage", {})

    # --- what answers the patient -------------------------------------------
    st.markdown("##### Language model")
    left, right = st.columns(2)
    left.metric(
        "Provider", PROVIDER_LABEL.get(model.get("provider", ""), model.get("provider", "?"))
    )
    right.metric("Effort", model.get("effort", "?"))

    _row("Assistant", model.get("agent_model"))
    _row("Safety classifier", model.get("classifier_model"))
    _row("Thinking", model.get("thinking"))
    _row(
        "Refusal fallbacks",
        "on" if model.get("server_side_fallbacks") else "off (not available on this provider)",
    )
    _row(
        "Credential",
        CREDENTIAL_LABEL.get(model.get("credential_source", ""), model.get("credential_source")),
    )

    st.divider()

    # --- what retrieves ------------------------------------------------------
    st.markdown("##### Knowledge base")
    status = knowledge.get("status", "unknown")
    if status == "ready":
        st.success(f"{knowledge.get('chunks', 0)} chunks indexed", icon="✅")
    else:
        st.warning(status, icon="⚠️")

    _row("Embeddings", knowledge.get("embedding_model"))
    _row("Provider", knowledge.get("embedding_provider"))
    _row("Vector store", knowledge.get("store", "—"))
    _row("Path", knowledge.get("vector_store_path"))
    _row("Minimum similarity", knowledge.get("min_similarity"))

    st.caption(
        "Retrieval is tiered. Treatment and dosage content is indexed but is not a "
        "candidate for any patient-facing search — the restriction is a filter on "
        "the query, not a rule in a prompt."
    )

    st.divider()

    # --- what the clinic decided --------------------------------------------
    st.markdown("##### Clinic policy")
    st.caption("Data, not code. Changing any of these needs a restart, not a deploy.")
    _row("Clinic", policy.get("clinic"))
    _row("Timezone", policy.get("timezone"))
    _row("Verification attempts", policy.get("verification_attempt_limit"))
    _row("Late-cancellation window", f"{policy.get('late_cancellation_hours')} hours")
    _row("Slots offered at once", policy.get("max_slots_presented"))
    _row("Emergency number", policy.get("emergency_number"))

    st.divider()

    # --- who else may be holding a session -----------------------------------
    st.markdown("##### Clinical review (spec r3)")
    clinical = config.get("clinical", {})
    if not clinical.get("enabled"):
        st.caption(
            "Off. No session can be established as clinical_assistant, and the "
            "four clinical-review functions are in no tool schema."
        )
    else:
        st.success("Enabled", icon="✅")
        _row("Session length", f"{clinical.get('session_minutes')} minutes")
        _row("Eligible channels", ", ".join(clinical.get("channels", [])) or "—")
        _row("Identity provider", clinical.get("directory"))
        _row(
            "Licensed roles",
            ", ".join(r.replace("_", " ") for r in clinical.get("permitted_roles", [])) or "—",
        )
        st.caption(
            "A patient-facing channel is never eligible, and that part is not "
            "configurable. No credential material is shown here or held by this "
            "process — the identity provider keeps the tokens."
        )

    st.divider()

    st.markdown("##### Service")
    _row("Version", service.get("version"))
    _row("Environment", service.get("environment"))
    _row("Session store", storage.get("database_url"))
    _row("Audit log", storage.get("audit_dir"))


def _row(label: str, value: Any) -> None:
    """A label and a value, wide enough to read a model id without wrapping."""
    left, right = st.columns([2, 3])
    left.caption(label)
    right.markdown(f"`{value}`" if value not in (None, "") else "—")
