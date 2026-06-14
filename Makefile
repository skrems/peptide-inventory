.PHONY: smoke docker-build docker-up docker-down

smoke:
	python3 scripts/smoke_test.py

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down
