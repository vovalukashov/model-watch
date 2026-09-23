import { describe, expect, it, vi } from 'vitest';

import { GATEWAY_MESSAGES_URL, handle, MAX_BODY_BYTES, MAX_TOKENS_CAP, type Deps } from '../lib/proxy.js';

const GOOD_BODY = {
  model: 'xiaomi/mimo-v2.6-flash',
  max_tokens: 4096,
  system: 'You write the alert text.',
  messages: [{ role: 'user', content: 'Changes found in the latest check:\n{}' }],
};

function deps(overrides: Partial<Deps> = {}) {
  const upstream = new Response(JSON.stringify({ type: 'message', content: [{ type: 'text', text: 'ok' }] }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
  return {
    verify: vi.fn(async () => ({})),
    gatewayToken: vi.fn(async () => 'vercel-oidc'),
    fetch: vi.fn(async () => upstream),
    ...overrides,
  } satisfies Deps;
}

function post(body: unknown, headers: Record<string, string> = { 'x-api-key': 'github-jwt' }) {
  return new Request('https://model-watch-ai.vercel.app/api/v1/messages', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}

async function errorOf(response: Response) {
  const body = await response.json();
  return { status: response.status, type: body.error?.type, message: body.error?.message as string };
}

describe('handle', () => {
  it('forwards an allowed request to AI Gateway with the Vercel OIDC token', async () => {
    const d = deps();
    const response = await handle(post({ ...GOOD_BODY, metadata: { user_id: 'x' }, tools: [{ name: 'shell' }] }), d);

    expect(response.status).toBe(200);
    expect(d.verify).toHaveBeenCalledWith('github-jwt');
    const [url, init] = vi.mocked(d.fetch).mock.calls[0];
    expect(url).toBe(GATEWAY_MESSAGES_URL);
    const headers = new Headers(init.headers);
    expect(headers.get('authorization')).toBe('Bearer vercel-oidc');
    expect(headers.get('anthropic-version')).toBe('2023-06-01');
    const sent = JSON.parse(String(init.body));
    expect(sent).toEqual({ ...GOOD_BODY, providerOptions: { gateway: { models: ['anthropic/claude-haiku-4.5'] } } });
  });

  it('passes the upstream status and body through', async () => {
    const d = deps({ fetch: vi.fn(async () => new Response('{"type":"error"}', { status: 529 })) });
    const response = await handle(post(GOOD_BODY), d);
    expect(response.status).toBe(529);
    expect(await response.text()).toBe('{"type":"error"}');
  });

  it('accepts the token as a bearer header too', async () => {
    const d = deps();
    await handle(post(GOOD_BODY, { authorization: 'Bearer github-jwt' }), d);
    expect(d.verify).toHaveBeenCalledWith('github-jwt');
  });

  it('does not add a fallback to the fallback model itself', async () => {
    const d = deps();
    await handle(post({ ...GOOD_BODY, model: 'anthropic/claude-haiku-4.5' }), d);
    const sent = JSON.parse(String(vi.mocked(d.fetch).mock.calls[0][1].body));
    expect(sent.providerOptions).toBeUndefined();
  });

  it('rejects requests without a token before anything else', async () => {
    const d = deps();
    expect(await errorOf(await handle(post(GOOD_BODY, {}), d))).toMatchObject({ status: 401, type: 'authentication_error' });
    expect(d.verify).not.toHaveBeenCalled();
    expect(d.fetch).not.toHaveBeenCalled();
  });

  it('rejects a token that fails verification and never reaches the gateway', async () => {
    const d = deps({ verify: vi.fn(async () => { throw new Error('wrong repository'); }) });
    const err = await errorOf(await handle(post(GOOD_BODY), d));
    expect(err).toMatchObject({ status: 401, type: 'authentication_error' });
    expect(err.message).toContain('wrong repository');
    expect(d.gatewayToken).not.toHaveBeenCalled();
    expect(d.fetch).not.toHaveBeenCalled();
  });

  it.each([
    ['a model outside the allowlist', { ...GOOD_BODY, model: 'anthropic/claude-opus-5' }, 'model'],
    ['max_tokens above the cap', { ...GOOD_BODY, max_tokens: MAX_TOKENS_CAP + 1 }, 'max_tokens'],
    ['a missing max_tokens', { ...GOOD_BODY, max_tokens: undefined }, 'max_tokens'],
    ['streaming', { ...GOOD_BODY, stream: true }, 'stream'],
    ['messages that are not a list', { ...GOOD_BODY, messages: 'hi' }, 'messages'],
  ])('rejects %s without calling the gateway', async (_name, body, field) => {
    const d = deps();
    const err = await errorOf(await handle(post(body), d));
    expect(err).toMatchObject({ status: 400, type: 'invalid_request_error' });
    expect(err.message).toContain(field);
    expect(d.fetch).not.toHaveBeenCalled();
  });

  it('rejects invalid JSON', async () => {
    expect((await errorOf(await handle(post('{nope'), deps()))).status).toBe(400);
  });

  it('rejects oversized bodies', async () => {
    const big = { ...GOOD_BODY, system: 'x'.repeat(MAX_BODY_BYTES) };
    expect((await errorOf(await handle(post(big), deps()))).status).toBe(413);
  });

  it('answers 503 when Vercel gave the function no OIDC token', async () => {
    const d = deps({ gatewayToken: vi.fn(async () => { throw new Error('missing'); }) });
    expect(await errorOf(await handle(post(GOOD_BODY), d))).toMatchObject({ status: 503, type: 'api_error' });
    expect(d.fetch).not.toHaveBeenCalled();
  });

  it('allows only POST', async () => {
    const response = await handle(new Request('https://model-watch-ai.vercel.app/api/v1/messages'), deps());
    expect(response.status).toBe(405);
  });
});
