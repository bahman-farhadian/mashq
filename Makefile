.DEFAULT_GOAL := help

.PHONY: help web

help: ## Show available Tartarus commands.
	@printf '%s\n' \
		'Tartarus commands:' \
		'  make web   Start the local web UI'

web: ## Start the localhost web UI.
	@python3 utils/tartarus_web.py $(opts)
