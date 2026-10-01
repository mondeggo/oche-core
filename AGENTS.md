# Project conventions

- Use English for code, comments, docstrings, UI copy, errors, configuration examples and documentation.
- Keep one source tree in `src/ochecore/`. Use `config/config.yaml` for startup settings and `docker-compose.yml` for deployment.
- Organize modules by responsibility and avoid unnecessary splitting. Keep `main.py` focused on application setup and lifecycle; route handlers belong in their own modules.
- Keep event models/parsing/dispatch in `events.py` and AutoDarts connection code in `autodarts/`.
- Keep the core headless. CLI and optional minimal UI use the same control API; authentication, persistence and event processing stay in the service.
- Use the AutoDarts cloud connection. Do not restore the removed legacy local-board adapter without a verified supported protocol.
- Preserve `.idea` and `ressources/` as the original user brief and reference material.
- Add integration modules when they are implemented; keep future plans in documentation instead of empty source files.
- After changes to connection behavior, run the relevant pytest tests and Ruff checks. Rebuild Docker when packaging or deployed files change.
- Use Conventional Commits (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`) with a short scope when useful. Commit completed, validated changes separately.
