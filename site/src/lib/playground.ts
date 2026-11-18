// The page's side of the playground module (site/playground). Exports return a reply, and send
// text back through the log channel: per piece, its length, then its bytes eight to a word.

import moduleUrl from '../wasm/fe_codec_playground.wasm?url';

type Reply = { ok: true; value: number } | { ok: false; code: number };

interface Guest {
  generateModule(models: string, eip712: boolean): Reply;
  encodeOrder(json: string): Reply;
  hashMail(json: string): Reply;
}

/** Names of fe_codec_core's error kinds, by their stable code. */
export const ERROR_KINDS = [
  '', 'UnexpectedEnd', 'CapacityExceeded', 'InvalidSyntax', 'InvalidUtf8', 'TypeMismatch',
  'IntegerOutOfRange', 'UnknownField', 'DuplicateField', 'MissingField', 'UnknownVariant',
  'InvalidLength', 'InvalidState', 'TrailingData', 'DepthLimit', 'CollectionLimit', 'Unsupported',
  'NonCanonical',
];

export interface CodecFailure {
  kind: string;
  offset: number;
  field: string;
}

export type Generated = { ok: true; text: string } | { ok: false; error: string };
export type Encoded = { ok: true; json: string; cbor: Uint8Array } | ({ ok: false } & CodecFailure);
export type Hashed =
  | { ok: true; generated: Uint8Array; generic: Uint8Array; digest: Uint8Array; request: string }
  | ({ ok: false } & CodecFailure);

const decoder = new TextDecoder();

export class Playground {
  private words: bigint[] = [];

  private constructor(private guest: Guest) {}

  static async load(): Promise<Playground> {
    // The bindings femp generated for the module, loaded with it only on this page.
    const { FeCodecPlayground } = await import('../wasm/fe_codec_playground.js');
    let target: Playground | undefined;
    const guest = await FeCodecPlayground.load(new URL(moduleUrl, window.location.href), {
      onLog: (value: number | bigint) => target?.words.push(BigInt(value)),
    });
    target = new Playground(guest);
    return target;
  }

  private pieces(): Uint8Array[] {
    const out: Uint8Array[] = [];
    const words = this.words;
    let i = 0;
    while (i < words.length) {
      const length = Number(words[i++]);
      const bytes = new Uint8Array(length);
      for (let b = 0; b < length; b++) {
        bytes[b] = Number((words[i + (b >> 3)] >> BigInt(8 * (b & 7))) & 0xffn);
      }
      i += Math.ceil(length / 8);
      out.push(bytes);
    }
    this.words = [];
    return out;
  }

  private failure(reply: Reply): CodecFailure {
    const [offset, field] = this.pieces();
    const code = reply.ok ? 0 : reply.code;
    return {
      kind: ERROR_KINDS[code] ?? `error ${code}`,
      offset: Number(decoder.decode(offset)),
      field: decoder.decode(field),
    };
  }

  generate(models: string, eip712: boolean): Generated {
    this.words = [];
    const reply = this.guest.generateModule(models, eip712);
    const [text = new Uint8Array()] = this.pieces();
    if (reply.ok && reply.value === 0) return { ok: true, text: decoder.decode(text) };
    return { ok: false, error: decoder.decode(text) };
  }

  encodeOrder(json: string): Encoded {
    this.words = [];
    const reply = this.guest.encodeOrder(json);
    if (!reply.ok) return { ok: false, ...this.failure(reply) };
    const [canonical, cbor] = this.pieces();
    return { ok: true, json: decoder.decode(canonical), cbor };
  }

  hashMail(json: string): Hashed {
    this.words = [];
    const reply = this.guest.hashMail(json);
    if (!reply.ok) return { ok: false, ...this.failure(reply) };
    const [generated, generic, digest, request] = this.pieces();
    return { ok: true, generated, generic, digest, request: decoder.decode(request) };
  }
}

export function hex(bytes: Uint8Array): string {
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}
