.PHONY: help brain-install brain-dev brain-check ui-install ui-dev ui-check wa-install wa-dev wa-typecheck db-init db-reset admin dev clean

help:
	@echo "Phoenix v2 — targets"
	@echo "  make brain-install   uv pip install -e brain/"
	@echo "  make brain-dev       uvicorn brain :8098"
	@echo "  make brain-check     smoke import test"
	@echo "  make wa-install      npm install en wa-listener"
	@echo "  make wa-dev          tsx watch listener (imprime QR la primera vez)"
	@echo "  make wa-typecheck    tsc --noEmit"
	@echo "  make db-init         crea DB y siembra SOUL default + grupo demo"
	@echo "  make db-reset        BORRA phoenix.db y re-siembra"
	@echo "  make admin           CLI interactivo (groups/souls/kbs)"
	@echo "  make clean           limpia __pycache__ y dist"

brain-install:
	cd brain && uv pip install -e .

brain-dev:
	cd brain && uvicorn phoenix_brain.server:app --reload --port 8102

brain-check:
	cd brain && python -c "from phoenix_brain import server, chat, soul, gating, sessions, models, config, anthropic_client, db; print('brain imports OK')"

ui-install:
	cd ui && uv pip install -e .

ui-dev:
	cd ui && uvicorn phoenix_ui.server:app --reload --port 8101

ui-check:
	cd ui && python -c "from phoenix_ui import server, clients, config; print('ui imports OK')"

dev:
	@echo "Arrancando brain :8102, listener :8100, ui :8101 (Ctrl-C mata todo)"
	@trap 'kill 0' INT TERM; \
	(cd brain && uvicorn phoenix_brain.server:app --reload --port 8102 2>&1 | sed 's/^/[brain] /') & \
	(cd wa-listener && npm run dev 2>&1 | sed 's/^/[wa] /') & \
	(cd ui && uvicorn phoenix_ui.server:app --reload --port 8101 2>&1 | sed 's/^/[ui] /') & \
	wait

wa-install:
	cd wa-listener && npm install

wa-dev:
	cd wa-listener && npm run dev

wa-typecheck:
	cd wa-listener && npx tsc --noEmit

db-init:
	cd brain && python -m phoenix_brain.db_init

db-reset:
	rm -f brain/phoenix.db
	$(MAKE) db-init

admin:
	cd brain && python -m phoenix_brain.admin_cli

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +
	rm -rf brain/dist wa-listener/dist
