"""Safety screening and the refusal set.

Specification §7 is a set of things that must happen *before* or *instead of*
normal routing. This package holds both:

* ``prescreen`` — emergency and advice detection, ahead of the agent loop
* ``refusals``  — the six topics handed to staff rather than answered
"""
