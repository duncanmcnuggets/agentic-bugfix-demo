# Repository instructions

## Commands

- Controller tests: `python -m pytest tests -q`
- Target public tests: `python -m pytest demo_target/tests -q`
- Lint: `python -m ruff check .`
- Type check: `python -m mypy bugfixer demo_target/src`
- Reproduce the intentional bug: `python demo_target/scripts/reproduce_bug.py`

## Safety and demo invariants

- The defect in `demo_target/src/config_service/resolver.py` is intentional on `main`.
- Do not fix the target while developing the control plane.
- Never read `.env`, `.env.local`, or the value of `OPENAI_API_KEY`.
- Do not make live API calls unless the user explicitly requests the live-run phase.
- Never commit secrets, push branches, create pull requests, merge, or deploy automatically.
- Models may receive only the `demo_target` repository view exposed by read-only tools.

