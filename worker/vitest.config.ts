import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: {
        configPath: "./wrangler.toml",
      },
      miniflare: {
        bindings: {
          HF_TOKEN: "test-hf-token",
          HF_REPO: "test-org/test-dataset",
          HF_API_BASE: "https://huggingface.co",
          TURNSTILE_SECRET: "test-turnstile-secret",
          TURNSTILE_VERIFY_URL: "https://turnstile.example.com/siteverify",
          NER_API_KEY: "test-ner-key",
          TURNSTILE_SITE_KEY: "test-site-key",
          KEY_COOLDOWN_SECONDS: "0",
          REG_RATE_LIMIT_PER_DAY: "1000",
          REG_INIT_RATE_LIMIT_PER_MINUTE: "1000",
        },
      },
    }),
  ],
  test: {
    include: ["src/**/*.test.{js,ts}", "test/**/*.test.{js,ts}"],
  },
});
