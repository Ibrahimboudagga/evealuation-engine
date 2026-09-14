# Testing

The test suite uses a local virtual environment and has no real provider
credential requirement. Its temporary files are written under `.pytest_tmp/`,
not the system temporary directory. The semantic-similarity test uses a local
stub, so it does not download a sentence-transformer model.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

## Verified environment

Recorded on 2026-09-14 with Windows and Python 3.14.6:

| Dependency | Version |
| --- | --- |
| pydantic | 2.13.5 |
| pydantic-settings | 2.15.0 |
| SQLAlchemy | 2.0.52 |
| openai | 3.13.0 |
| anthropic | 1.5.0 |
| google-generativeai | 0.8.6 |
| cohere | 7.1.1 |
| fastapi | 0.141.1 |
| uvicorn | 0.53.0 |
| gradio | 6.27.0 |
| httpx | 0.28.1 |
| pytest | 9.1.1 |
| pytest-asyncio | 1.4.0 |
| numpy | 2.5.3 |
| sentence-transformers | 6.0.1 |
| structlog | 26.1.0 |
| json_repair | 0.63.4 |

## Deterministic providers

`tests.fakes.DeterministicFakeProvider` is available for tests that need a
successful completion, valid single or pairwise judge JSON, malformed JSON, or
a provider exception. It records prompts and never makes a network request.
