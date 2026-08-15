.PHONY: dev down rebuild-db test validate

dev:
	docker compose up --build --wait
	@ui_port=$$(docker compose port web 5173 | sed 's/.*://'); \
		echo "Open the UI: http://localhost:$${ui_port}"

down:
	docker compose down

rebuild-db:
	docker compose down -v
	docker compose up -d mysql
	docker compose run --rm api alembic upgrade head

test:
	docker compose run --rm api pytest
	docker compose run --rm web npm test -- --run

validate:
	docker compose run --rm api elections-data validate
