VENV   := .venv
PYTHON := $(VENV)/bin/python3
PIP    := $(VENV)/bin/pip

.PHONY: install run calibrate clean

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --quiet -r requirements.txt

install: $(VENV)/bin/activate

run: install
	$(PYTHON) spankux.py $(ARGS)

calibrate: install
	$(PYTHON) calibrate.py $(ARGS)

clean:
	rm -rf $(VENV) __pycache__ profile.json
