# Walacor AI Security Gateway

.PHONY: install run run-prod test lint provision-walacor verify-install

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

# Provision Walacor audit schemas on the backend named in .env.
# Idempotent — safe to re-run on an already-provisioned tenant.
provision-walacor:
	./scripts/provision-walacor.sh

# Post-`docker compose up` sanity: container env populated, /health OK,
# signing key generated, Walacor delivery green. Reports gaps with exit 1.
verify-install:
	./scripts/verify-install.sh
