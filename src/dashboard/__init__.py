"""Streamlit dashboard (architecture Section 5.7).

Reads only from published pipeline output (never touches raw/processed
pipeline internals directly) -- a clean boundary that lets the dashboard
stay ignorant of pipeline implementation details.
"""
