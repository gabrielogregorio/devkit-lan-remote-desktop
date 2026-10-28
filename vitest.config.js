import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["web/__tests__/**/*.test.js"],
    setupFiles: ["web/__tests__/setup.js"],
    coverage: {
      provider: "v8",
      include: ["web/app.js", "web/i18n.js"],
      reporter: ["text", "html"],
      thresholds: { lines: 95, functions: 95, branches: 90, statements: 95 },
    },
  },
});
