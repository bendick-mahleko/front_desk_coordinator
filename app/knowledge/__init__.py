"""The disease knowledge base.

Retrieval is tiered: every chunk carries an audience decided at build time, and
a search must name the tiers it is permitted to read. Clinical treatment and
dosage content exists in the index but is unreachable from any patient-facing
tool, because the restriction is a filter on the query rather than a rule in a
prompt.
"""
