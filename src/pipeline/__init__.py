"""Orchestrator (architecture Section 5.6).

A single Python entrypoint (run.py) calling acquisition -> normalize ->
spatial join/grid -> scoring -> publish in sequence. No workflow
orchestration framework -- a weekly single-container job doesn't need one
(architecture Section 1/10).
"""
