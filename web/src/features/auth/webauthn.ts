/** The browser side of passkeys: JSON from the NVR in, JSON back out, with the buffers in between. */

interface DescriptorJson { type: "public-key"; id: string; transports?: AuthenticatorTransport[] }

export interface CreationOptionsJson {
  challenge: string;
  rp: PublicKeyCredentialRpEntity;
  user: { id: string; name: string; displayName: string };
  pubKeyCredParams: PublicKeyCredentialParameters[];
  excludeCredentials: DescriptorJson[];
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  attestation?: AttestationConveyancePreference;
  timeout?: number;
}

export interface RequestOptionsJson {
  challenge: string;
  rpId: string;
  userVerification?: UserVerificationRequirement;
  allowCredentials?: DescriptorJson[];
  timeout?: number;
}

export interface CredentialJson { id: string; rawId: string; type: string; response: Record<string, unknown> }

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && "PublicKeyCredential" in window && !!navigator.credentials;
}

function decode(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes.buffer;
}

function encode(buffer: ArrayBuffer | null | undefined): string | null {
  if (!buffer) return null;
  let binary = "";
  for (const byte of new Uint8Array(buffer)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const descriptors = (items: DescriptorJson[] | undefined): PublicKeyCredentialDescriptor[] =>
  (items ?? []).map(item => ({ type: item.type, id: decode(item.id), transports: item.transports }));

/** A browser refusal in words, for the one that is the person's own doing. */
function explain(error: unknown): Error {
  if (error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "AbortError")) {
    return new Error("The passkey request was cancelled or timed out.");
  }
  if (error instanceof DOMException && error.name === "InvalidStateError") return new Error("This passkey is already registered.");
  if (error instanceof DOMException && error.name === "SecurityError") return new Error("Passkeys do not work at this web address. Open the NVR at its normal address.");
  return error instanceof Error ? error : new Error("The passkey request failed.");
}

export async function createPasskey(options: CreationOptionsJson): Promise<CredentialJson> {
  let credential: Credential | null;
  try {
    credential = await navigator.credentials.create({
      publicKey: {
        ...options,
        challenge: decode(options.challenge),
        user: { ...options.user, id: decode(options.user.id) },
        excludeCredentials: descriptors(options.excludeCredentials),
      },
    });
  } catch (error) {
    throw explain(error);
  }
  if (!(credential instanceof PublicKeyCredential)) throw new Error("No passkey was created.");
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: encode(credential.rawId) ?? "",
    type: credential.type,
    response: { clientDataJSON: encode(response.clientDataJSON), attestationObject: encode(response.attestationObject), transports: response.getTransports?.() ?? [] },
  };
}

export async function getPasskey(options: RequestOptionsJson): Promise<CredentialJson> {
  let credential: Credential | null;
  try {
    credential = await navigator.credentials.get({
      publicKey: { ...options, challenge: decode(options.challenge), allowCredentials: descriptors(options.allowCredentials) },
    });
  } catch (error) {
    throw explain(error);
  }
  if (!(credential instanceof PublicKeyCredential)) throw new Error("No passkey was provided.");
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: encode(credential.rawId) ?? "",
    type: credential.type,
    response: { clientDataJSON: encode(response.clientDataJSON), authenticatorData: encode(response.authenticatorData), signature: encode(response.signature), userHandle: encode(response.userHandle) },
  };
}
