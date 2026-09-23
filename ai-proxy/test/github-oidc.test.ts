import { createLocalJWKSet, exportJWK, generateKeyPair, SignJWT, type JWTPayload } from 'jose';
import { beforeAll, describe, expect, it } from 'vitest';

import { AuthError, AUDIENCE, GITHUB_ISSUER, verifyGithubToken } from '../lib/github-oidc.js';

const GOOD_CLAIMS = {
  repository: 'vovalukashov/model-watch',
  repository_id: '1381793078',
  ref: 'refs/heads/main',
  workflow_ref: 'vovalukashov/model-watch/.github/workflows/watch.yml@refs/heads/main',
};

let privateKey: CryptoKey;
let foreignKey: CryptoKey;
let keys: ReturnType<typeof createLocalJWKSet>;

beforeAll(async () => {
  const pair = await generateKeyPair('RS256');
  privateKey = pair.privateKey;
  foreignKey = (await generateKeyPair('RS256')).privateKey;
  const jwk = { ...(await exportJWK(pair.publicKey)), kid: 'test', alg: 'RS256' };
  keys = createLocalJWKSet({ keys: [jwk] });
});

async function token(claims: JWTPayload = GOOD_CLAIMS, opts: { issuer?: string; audience?: string; key?: CryptoKey; expiresIn?: string } = {}) {
  return new SignJWT(claims)
    .setProtectedHeader({ alg: 'RS256', kid: 'test' })
    .setIssuer(opts.issuer ?? GITHUB_ISSUER)
    .setAudience(opts.audience ?? AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(opts.expiresIn ?? '5m')
    .sign(opts.key ?? privateKey);
}

describe('verifyGithubToken', () => {
  it('accepts a token GitHub issued to this repository, branch and workflow', async () => {
    const payload = await verifyGithubToken(await token(), keys);
    expect(payload.repository).toBe('vovalukashov/model-watch');
  });

  it.each([
    ['another issuer', { issuer: 'https://evil.example' }],
    ['another audience', { audience: 'someone-else' }],
    ['an expired token', { expiresIn: '-1m' }],
    ['a foreign signing key', { key: undefined as unknown as CryptoKey, foreign: true }],
  ])('rejects %s', async (_name, opts) => {
    const signed = 'foreign' in opts ? await token(GOOD_CLAIMS, { key: foreignKey }) : await token(GOOD_CLAIMS, opts);
    await expect(verifyGithubToken(signed, keys)).rejects.toBeInstanceOf(AuthError);
  });

  it.each([
    ['another repository', { repository: 'someone/model-watch' }],
    ['a recreated repository with the same name', { repository_id: '1' }],
    ['another branch', { ref: 'refs/heads/feature' }],
    ['another workflow file', { workflow_ref: 'vovalukashov/model-watch/.github/workflows/evil.yml@refs/heads/main' }],
  ])('rejects %s', async (_name, override) => {
    await expect(verifyGithubToken(await token({ ...GOOD_CLAIMS, ...override }), keys)).rejects.toBeInstanceOf(AuthError);
  });

  it('rejects garbage', async () => {
    await expect(verifyGithubToken('not-a-jwt', keys)).rejects.toBeInstanceOf(AuthError);
  });
});
