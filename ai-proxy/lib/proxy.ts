export const GATEWAY_MESSAGES_URL = 'https://ai-gateway.vercel.sh/v1/messages';
export const ALLOWED_MODELS = ['xiaomi/mimo-v2.6-flash', 'anthropic/claude-haiku-4.5'];
export const FALLBACK_MODELS = ['anthropic/claude-haiku-4.5']; // tried by AI Gateway when the primary fails
export const MAX_TOKENS_CAP = 4096;
export const MAX_BODY_BYTES = 32_768;

export type Deps = {
  verify: (token: string) => Promise<unknown>;
  gatewayToken: () => Promise<string>;
  fetch: (url: string, init: RequestInit) => Promise<Response>;
};

type ErrorType = 'authentication_error' | 'invalid_request_error' | 'request_too_large' | 'api_error';

/** Anthropic-shaped errors, so the client SDK raises the right exception. */
function fail(status: number, type: ErrorType, message: string): Response {
  return Response.json({ type: 'error', error: { type, message } }, { status });
}

function bearer(header: string | null): string | null {
  const match = header?.match(/^Bearer\s+(.+)$/i);
  return match ? match[1] : null;
}

/**
 * An Anthropic Messages endpoint that only the model-watch workflow can use: it checks the caller's
 * GitHub OIDC token, keeps the request to an allowlisted model and a small budget, and forwards it
 * to AI Gateway with this deployment's own Vercel OIDC token. No API key exists anywhere.
 */
export async function handle(request: Request, deps: Deps): Promise<Response> {
  if (request.method !== 'POST') {
    return fail(405, 'invalid_request_error', 'only POST is supported');
  }
  const token = request.headers.get('x-api-key') ?? bearer(request.headers.get('authorization'));
  if (!token) {
    return fail(401, 'authentication_error', 'missing GitHub OIDC token');
  }
  try {
    await deps.verify(token);
  } catch (error) {
    return fail(401, 'authentication_error', (error as Error).message);
  }

  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) {
    return fail(413, 'request_too_large', `body is over ${MAX_BODY_BYTES} bytes`);
  }
  let body: Record<string, unknown>;
  try {
    body = JSON.parse(raw);
  } catch {
    return fail(400, 'invalid_request_error', 'body is not valid JSON');
  }
  const { model, max_tokens, messages, system, stream } = body;
  if (typeof model !== 'string' || !ALLOWED_MODELS.includes(model)) {
    return fail(400, 'invalid_request_error', `model is not allowed: ${String(model)}`);
  }
  if (!Number.isInteger(max_tokens) || (max_tokens as number) < 1 || (max_tokens as number) > MAX_TOKENS_CAP) {
    return fail(400, 'invalid_request_error', `max_tokens must be an integer from 1 to ${MAX_TOKENS_CAP}`);
  }
  if (stream) {
    return fail(400, 'invalid_request_error', 'stream is not supported');
  }
  if (!Array.isArray(messages)) {
    return fail(400, 'invalid_request_error', 'messages must be a list');
  }

  const fallbacks = FALLBACK_MODELS.filter((m) => m !== model);
  const forward = {
    model,
    max_tokens,
    ...(system !== undefined && { system }),
    messages,
    ...(fallbacks.length > 0 && { providerOptions: { gateway: { models: fallbacks } } }),
  };

  let gatewayToken: string;
  try {
    gatewayToken = await deps.gatewayToken();
  } catch {
    return fail(503, 'api_error', 'this deployment has no Vercel OIDC token');
  }

  const upstream = await deps.fetch(GATEWAY_MESSAGES_URL, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${gatewayToken}`,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify(forward),
  });
  return new Response(await upstream.text(), {
    status: upstream.status,
    headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
  });
}
