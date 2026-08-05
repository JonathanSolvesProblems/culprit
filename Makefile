# Culprit: one-command reproduction.
#
# A judge with Docker and Python 3.11/3.12 should be able to run `make demo`
# and end up with a working DataHub instance, a real warehouse, a trained
# model, full ML lineage in the graph, and a completed investigation.

# python3 on POSIX, python on Windows where python3 is usually absent.
PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
ifeq ($(OS),Windows_NT)
	BIN := $(VENV)/Scripts
	PY := python
endif

.PHONY: help install datahub data transform ingest train score lineage investigate demo verify examples clean

help:
	@echo "make install      create venv and install dependencies"
	@echo "make datahub      start DataHub OSS locally (Docker)"
	@echo "make data         download real NYC TLC data into DuckDB"
	@echo "make transform    run dbt build + docs generate"
	@echo "make ingest       ingest dbt lineage into DataHub"
	@echo "make train        train production model + counterfactual control"
	@echo "make score        score the real 2025-06 month"
	@echo "make lineage      emit ML entities into DataHub"
	@echo "make investigate  run the Culprit agent (needs any provider key, see .env.example)"
	@echo "make fix          generate the repair, prove it with dbt, optionally open a PR"
	@echo "make replay       render a recorded real run, no API key needed"
	@echo "make verify       prove the incident is real, no DataHub needed"
	@echo "make demo         everything above, in order"

install:
	@test -d $(VENV) || $(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -r requirements.txt
	$(BIN)/python -m pip install mcp-server-datahub

datahub:
	$(BIN)/datahub docker quickstart

data:
	$(BIN)/python pipeline/load_raw.py

transform:
	cd pipeline/dbt && DBT_PROFILES_DIR=. ../../$(BIN)/dbt build
	cd pipeline/dbt && DBT_PROFILES_DIR=. ../../$(BIN)/dbt docs generate

ingest:
	cd pipeline && ../$(BIN)/datahub ingest -c ingest_dbt.yml

train:
	$(BIN)/python pipeline/train_model.py

score:
	$(BIN)/python pipeline/score_batch.py --month 2025-06

lineage:
	$(BIN)/python pipeline/emit_ml_lineage.py

investigate: install
	$(BIN)/python -m culprit.cli investigate --write-back

fix: install
	$(BIN)/python -m culprit.cli fix

replay: install
	$(BIN)/python -m culprit.cli replay --animate

examples: install
	$(BIN)/python scripts/generate_examples.py

# Independent verification that the incident is real. Needs no DataHub and no
# API key. Run this first if you only have a few minutes.
verify: install
	$(BIN)/python scripts/scan_tlc_semantics.py
	$(BIN)/python scripts/validate_impact.py

demo: install datahub data transform ingest train score lineage investigate

clean:
	rm -f pipeline/warehouse.duckdb pipeline/warehouse.duckdb.wal
	rm -rf pipeline/dbt/target pipeline/artifacts/*.pkl
