"""Local web UI for duet (PLAN §6, W2 + core W3).

A single FastAPI service that renders the session log in a browser and answers
gates there, so a debate can be watched and approved without the terminal. The
engine is untouched: the web app is just another client of the same event log
the CLI reads, exactly as the architecture (gates as events, log as projection)
was built for.

This is the local, single-process cut. The Next.js + FastAPI split and the job
queue in PLAN §6.3.6 are the hosted, multi-user path; the seam is the same event
log, so that remains a front-end swap rather than an engine change.
"""
