# ai-proxy

An Anthropic Messages endpoint on Vercel that only the model-watch workflow can use. It lets the watcher
call AI Gateway without any API key:

1. The workflow mints a GitHub Actions OIDC token for the audience `model-watch-ai` (`id-token: write`)
   and sends it as the Anthropic SDK's API key.
2. `lib/github-oidc.ts` verifies the token against GitHub's JWKS and requires repository
   `vovalukashov/model-watch` (by name and numeric id), ref `refs/heads/main` and workflow `watch.yml`.
3. `lib/proxy.ts` accepts only `xiaomi/mimo-v2.6-flash` and `anthropic/claude-haiku-4.5`, at most
   4096 output tokens and a 32 KB body, drops every other field, adds the Haiku fallback and forwards
   the request to AI Gateway with the deployment's own Vercel OIDC token.

The AI Gateway budget for the `model-watch-ai` project is capped at $1 a month.

```bash
pnpm install
pnpm test && pnpm typecheck
vercel deploy --prod          # from this directory; not connected to Git on purpose,
                              # so snapshot commits do not trigger builds
```

Point the watcher at it with the repository variable `AI_PROXY_URL=https://<production domain>/api`.
