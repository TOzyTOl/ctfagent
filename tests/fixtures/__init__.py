"""Fixture challenges for the offline test suite.

These are real, self-contained challenges: a deterministic crypto puzzle, a
reversed Python script, and a forensics archive. They exist so the end-to-end
test can prove the orchestration loop (dispatch -> worker -> flag -> verify ->
confirm -> writeup) without an LLM in the loop: ``ScriptedBackend`` plays the
agents, and the fixtures give those scripts something concrete to point at.
"""
