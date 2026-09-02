# SIH26183 developer entrypoints. Nothing here touches git.
.DEFAULT_GOAL := help
COMPOSE := docker compose

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the full stack
	$(COMPOSE) up -d --build

down: ## Stop the stack (volumes preserved)
	$(COMPOSE) down

nuke: ## Stop the stack and DELETE all volumes
	$(COMPOSE) down -v

logs: ## Tail logs from all services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

health: ## Curl the backend readiness probe
	curl -s http://localhost:$(or $(BACKEND_PORT),8001)/api/v1/health/ready | python -m json.tool

ports: ## Show host port bindings for this stack
	$(COMPOSE) ps --format 'table {{.Name}}	{{.Ports}}'

test: ## Run backend unit tests inside the backend container
	$(COMPOSE) exec backend pytest -q

lint-frontend: ## Run ESLint (the JS static gate - there is no tsc here)
	$(COMPOSE) exec frontend npm run lint

.PHONY: help up down nuke logs ps ports health test lint-frontend
