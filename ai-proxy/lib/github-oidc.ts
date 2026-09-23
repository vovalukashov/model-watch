import { createRemoteJWKSet, jwtVerify, type JWTPayload, type JWTVerifyGetKey } from 'jose';

export const GITHUB_ISSUER = 'https://token.actions.githubusercontent.com';
export const AUDIENCE = 'model-watch-ai';

// Only the watcher workflow on main may spend AI Gateway credits. The numeric id guards against
// someone recreating a deleted repository under the same name.
const REPOSITORY = 'vovalukashov/model-watch';
const REPOSITORY_ID = '1381793078';
const REF = 'refs/heads/main';
const WORKFLOW_PREFIX = `${REPOSITORY}/.github/workflows/watch.yml@`;

const githubKeys = createRemoteJWKSet(new URL(`${GITHUB_ISSUER}/.well-known/jwks`));

export class AuthError extends Error {}

/** Verifies a GitHub Actions OIDC token and checks that it was minted for the model-watch workflow. */
export async function verifyGithubToken(token: string, keys: JWTVerifyGetKey = githubKeys): Promise<JWTPayload> {
  let payload: JWTPayload;
  try {
    ({ payload } = await jwtVerify(token, keys, { issuer: GITHUB_ISSUER, audience: AUDIENCE, algorithms: ['RS256'] }));
  } catch (error) {
    throw new AuthError(`invalid GitHub OIDC token: ${(error as Error).message}`);
  }
  if (payload.repository !== REPOSITORY || String(payload.repository_id) !== REPOSITORY_ID) {
    throw new AuthError('token was issued to another repository');
  }
  if (payload.ref !== REF) {
    throw new AuthError('token was issued to another branch');
  }
  if (typeof payload.workflow_ref !== 'string' || !payload.workflow_ref.startsWith(WORKFLOW_PREFIX)) {
    throw new AuthError('token was issued to another workflow');
  }
  return payload;
}
