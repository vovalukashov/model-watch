import { getVercelOidcTokenSync } from '@vercel/oidc';

import { verifyGithubToken } from '../../lib/github-oidc.js';
import { handle } from '../../lib/proxy.js';

// Served at /api/v1/messages, so the Anthropic SDK works with base_url=https://<deployment>/api.
export default {
  fetch(request: Request) {
    return handle(request, {
      verify: (token) => verifyGithubToken(token),
      // A running function gets its OIDC token as a request header, not as an environment variable.
      gatewayToken: async () => request.headers.get('x-vercel-oidc-token') ?? getVercelOidcTokenSync(),
      fetch: (url, init) => fetch(url, init),
    });
  },
};
