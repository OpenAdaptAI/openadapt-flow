SHELL := /bin/sh
BUILD_DIR := build

.PHONY: all check clean

all: check
	mkdir -p $(BUILD_DIR)
	latexmk -pdf -interaction=nonstopmode -halt-on-error \
		-output-directory=$(BUILD_DIR) main.tex

check:
	python3 check_artifacts.py

clean:
	latexmk -C -output-directory=$(BUILD_DIR) main.tex 2>/dev/null || true
	rm -rf $(BUILD_DIR)
