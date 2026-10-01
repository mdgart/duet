# Isolated web-client checks

These tests exercise the shipped HTML and JavaScript with jsdom and Node's built-in test runner. Every API response and event stream is simulated. They do not start a browser or server, call a model, or change real sessions.

Use Node 22.22.2+, Node 24.15+, or Node 26+, and install the test-only DOM dependency in a temporary directory:

```sh
duet_test_dir=$(mktemp -d)
npm install --prefix "$duet_test_dir" --no-audit --no-fund jsdom@30.1.1
NODE_PATH="$duet_test_dir/node_modules" node --test tests/web/client.test.cjs
```

Run from the repository root. No Node dependency or build step is added to the application. Reuse that temporary dependency directory for subsequent runs.

The checks cover Markdown safety and structure, review identity and double-submission, external changes after an event stream ends, edit preservation, fractional-cent budget validation, mobile navigation focus/inert state, canonical document export, new-debate fields, and late navigation responses. They verify client behavior with controlled data; visual layout, browser accessibility trees, native dialog focus, downloaded-file delivery, and live provider integrations require separate browser/end-to-end verification.
