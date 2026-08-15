SINGLE_MEMBER_TOTALS ?= data/published-single-member-totals-2021.json

.PHONY: dev down rebuild-db rebuild-data test validate verify-complete

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

rebuild-data:
	@test -f "$(SINGLE_MEMBER_TOTALS)" || { \
		echo "Missing official single-member reference: $(SINGLE_MEMBER_TOTALS)"; \
		echo "Set SINGLE_MEMBER_TOTALS to a local file documented in docs/data-pipeline.md"; \
		exit 2; \
	}
	docker compose run --rm api elections-data migrate
	docker compose run --rm api elections-data download
	docker compose run --rm api elections-data acquire-single-member
	docker compose run --rm api elections-data import-results
	docker compose run --rm api elections-data import-single-member
	docker compose run --rm api elections-data import-commissions
	docker compose run --rm api elections-data match
	docker compose run --rm api elections-data validate \
		--published-totals data/published-totals-2021.json \
		--single-member-published-totals "$(SINGLE_MEMBER_TOTALS)"
	docker compose run --rm api elections-data verify-complete

test:
	docker compose run --rm api pytest
	docker compose run --rm web npm test -- --run

validate:
	docker compose run --rm api elections-data validate

verify-complete:
	docker compose run --rm api elections-data verify-complete
