.PHONY: check test lint build audit

check: test lint audit

test:
	python3 -m unittest discover -s tests -p 'test_*.py' -v
	npm test

lint:
	python3 -m json.tool plugins/chat-room/.codex-plugin/plugin.json >/dev/null
	python3 -m json.tool .agents/plugins/marketplace.json >/dev/null
	npm run lint

build:
	npm run build

audit:
	npm audit --omit=dev
