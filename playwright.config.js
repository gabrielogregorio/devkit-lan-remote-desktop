import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "web/e2e",
  // 127.0.0.1, not localhost: here localhost resolves to ::1 first and the server listens on IPv4.
  timeout: 30000,
  use: {
    baseURL: "https://127.0.0.1:18443",
    ignoreHTTPSErrors: true,
    headless: true,
  },
  webServer: {
    // The real server, made safe for tests: LRD_INPUT=off never opens /dev/uinput (the e2e must
    // never move the real mouse) and LRD_GAMEPAD_MAP keeps saved profiles in test-results/
    // (git-ignored) instead of the developer's real ~/.config file.
    command:
      "LRD_GUARDIAN=off LRD_INPUT=off LRD_GAMEPAD_MAP=test-results/e2e-gamepad-map.json LRD_PORT=18443 LRD_CERT=certs/cert.pem LRD_KEY=certs/key.pem ./.venv/bin/python -m server.main",
    url: "https://127.0.0.1:18443",
    ignoreHTTPSErrors: true,
    reuseExistingServer: !process.env.CI,
    timeout: 20000,
  },
});
