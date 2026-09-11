"""Deterministic scheduling. See docs/DESIGN.md §7 for the split between
LLM judgment (an effort estimate, an input) and this — plain, testable
code that turns estimates + availability into concrete calendar blocks.
"""
