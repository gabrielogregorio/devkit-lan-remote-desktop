import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "web/e2e",
  timeout: 30000,
  use: {
    baseURL: "https://127.0.0.1:18443",
    ignoreHTTPSErrors: true,
    headless: true,
  },
  webServer: {
    command:
      "RL_GUARDIAN=off RL_PORT=18443 RL_CERT=certs/cert.pem RL_KEY=certs/key.pem ./.venv/bin/python -m server.main",
    url: "https://127.0.0.1:18443",
    ignoreHTTPSErrors: true,
    reuseExistingServer: true,
    timeout: 20000,
  },
});
