Commands

- `uv sync` - install dependencies
- `uv run pytest` - the whole suite
- `uv run pytest tests/test_home.py` - one test file

Rules

- Dependencies are added in `pyproject.toml`. Add what the task needs
  without asking, and keep the list minimal

Documents

- `_docs/process.md` - how work is organized