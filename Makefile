# Walacor AI Security Gateway

.PHONY: install run run-prod test lint

install:
	pip install -e ../walacor-core
	pip install -e .

run:
	uvicorn gateway.main:app --host 0.0.0.0 --port 8000 --reload

run-prod:
	walacor-gateway

test:
	cd .. && PYTHONPATH=Gateway/src pytest Gateway/tests -v

lint:
	ruff check src/gateway
