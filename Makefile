.PHONY: run dev test seed migrate downgrade lint fmt check install

# ---- Dev server -------------------------------------------------------
run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

dev:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# ---- Tests ------------------------------------------------------------
test:
	python -m pytest -v --tb=short

test-cov:
	python -m pytest -v --tb=short --cov=app --cov-report=term-missing

# ---- Database ---------------------------------------------------------
migrate:
	alembic upgrade head

downgrade:
	alembic downgrade -1

revision:
	alembic revision --autogenerate -m "$(msg)"

seed:
	python -m scripts.seed

# ---- Code quality -----------------------------------------------------
lint:
	python -m ruff check app tests scripts
	python -m black --check app tests scripts

fmt:
	python -m ruff check --fix app tests scripts
	python -m black app tests scripts

# ---- Install ----------------------------------------------------------
install:
	pip install -e ".[dev]"
