.PHONY: help brain-install brain-dev brain-check ui-install ui-dev wa-install wa-typecheck relay-install relay-dev relay-test db-migrate db-downgrade verify dev clean

help:
	@echo "one-on-one — targets disponibles:"
	@echo "  make brain-install   instala deps del brain (uv pip install -e brain/)"
	@echo "  make brain-dev       arranca brain en puerto 8096"
	@echo "  make brain-check     import smoke test del brain"
	@echo "  make ui-install      instala deps de la UI"
	@echo "  make ui-dev          arranca UI en puerto 8097"
	@echo "  make wa-install      npm install en wa-listener"
	@echo "  make wa-dev          tsx watch listener (imprime QR la primera vez)"
	@echo "  make wa-typecheck    tsc --noEmit en wa-listener"
	@echo "  make relay-install   instala deps del relay-bot Telegram"
	@echo "  make relay-dev       arranca relay-bot en puerto 8098"
	@echo "  make relay-test      pytest del relay-bot"
	@echo "  make db-migrate      alembic upgrade head"
	@echo "  make db-downgrade    alembic downgrade -1"
	@echo "  make dev             arranca brain + ui + listener + relay en paralelo"
	@echo "  make verify          ejecuta py_compile sobre todo el scaffolding"
	@echo "  make clean           limpia __pycache__ y dist"

brain-install:
	cd brain && uv pip install -e .

brain-dev:
	cd brain && uvicorn iris_brain.server:app --reload --port 8096

brain-check:
	cd brain && python -c "from iris_brain import server, chat, safety, intents, tools, models, sessions, soul, relay, config, db; print('brain imports OK')"

ui-install:
	cd ui && uv pip install -e .

ui-dev:
	cd ui && uvicorn iris_ui.server:app --reload --port 8097

wa-install:
	cd wa-listener && npm install

wa-dev:
	cd wa-listener && npx tsx src/index.ts

wa-typecheck:
	cd wa-listener && npx tsc --noEmit

relay-install:
	cd relay-bot && uv pip install -e .[dev]

relay-dev:
	cd relay-bot && uvicorn iris_relay.server:app --reload --port 8098

relay-test:
	cd relay-bot && pytest -q

db-migrate:
	cd db && alembic upgrade head

db-downgrade:
	cd db && alembic downgrade -1

dev:
	@echo "Arrancando brain :8096, listener :8099, relay :8098, ui :8097 (Ctrl-C mata todo)"
	@trap 'kill 0' INT TERM; \
	(cd brain && uvicorn iris_brain.server:app --reload --port 8096 2>&1 | sed 's/^/[brain] /') & \
	(cd wa-listener && npx tsx src/index.ts 2>&1 | sed 's/^/[wa] /') & \
	(cd relay-bot && uvicorn iris_relay.server:app --reload --port 8098 2>&1 | sed 's/^/[relay] /') & \
	(cd ui && uvicorn iris_ui.server:app --reload --port 8097 2>&1 | sed 's/^/[ui] /') & \
	wait

verify:
	@echo "=== Python syntax check (brain) ==="
	@find brain/src -name '*.py' -exec python -m py_compile {} \;
	@echo "brain py_compile OK"
	@echo "=== Python syntax check (ui) ==="
	@find ui/src -name '*.py' -exec python -m py_compile {} \;
	@echo "ui py_compile OK"
	@echo "=== Python syntax check (relay-bot) ==="
	@find relay-bot/src -name '*.py' -exec python -m py_compile {} \;
	@echo "relay-bot py_compile OK"
	@echo "=== Python syntax check (alembic versions) ==="
	@find db/alembic/versions -name '*.py' -exec python -m py_compile {} \;
	@echo "alembic py_compile OK"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf wa-listener/dist
	@echo "clean OK"
