import { verifyAttestation, verifyAssertion } from 'node-app-attest';
import { X509Certificate } from 'node:crypto';
import cbor from 'cbor';

export function verifyRegistration(input, payload, previous, env) {
  const common = { bundleIdentifier: env.APP_BUNDLE_ID, teamIdentifier: env.APPLE_TEAM_ID };
  if (previous) {
    const result = verifyAssertion({ ...common, assertion: Buffer.from(input.assertion, 'base64'),
      payload, publicKey: previous.publicKey, signCount: previous.signCount });
    return { ...previous, signCount: result.signCount };
  }
  const attestation = Buffer.from(input.attestation, 'base64');
  const decoded = cbor.decodeAllSync(attestation);
  if (decoded.length !== 1) throw new Error('Invalid attestation');
  for (const raw of decoded[0].attStmt.x5c) {
    const certificate = new X509Certificate(raw);
    if (Date.parse(certificate.validFrom) > Date.now() || Date.parse(certificate.validTo) <= Date.now()) {
      throw new Error('Expired attestation');
    }
  }
  const result = verifyAttestation({ ...common, attestation, challenge: payload, keyId: input.key_id,
    allowDevelopmentEnvironment: env.ALLOW_DEVELOPMENT_ATTESTATION === 'true' });
  return { publicKey: result.publicKey, environment: result.environment, signCount: 0 };
}
