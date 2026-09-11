"""Pluggable effort-estimate backends: claude, openai_compat (Groq,
Mistral, OpenRouter, GitHub Models, or any OpenAI-compatible endpoint —
including a local Ollama server, if you'd rather use that than
llama-cpp-python), and local (llama-cpp-python, fully offline).

Every backend exposes the same shape: an async `estimate(course_name,
assignment_row, types) -> (p50_minutes, p80_minutes)`. llm_estimate.py's
caching/orchestration logic doesn't know or care which one it's holding.
"""
