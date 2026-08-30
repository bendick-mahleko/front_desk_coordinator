"""The policy core — where specification §3 becomes enforceable code.

Three modules carry the safety argument:

* ``gates``        the authorization table and the four-check evaluator
* ``verification`` the identity state machine and attempt limits
* ``provenance``   the ledger that rejects fabricated identifiers

If those are correct and ``decorator.gated`` is applied to all fifteen tool
functions, no prompt change and no model behaviour can produce an unauthorised
disclosure.
"""
