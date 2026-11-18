// Build-time values: the playground's WebAssembly module, run in Node while the site builds,
// computes every byte and hash the pages show, so the site cannot drift from Fe Codec. A page
// whose text no longer matches what the library produces fails the build.

import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { MAIL, ORDER } from './samples';

const decoder = new TextDecoder();

export interface SiteValues {
  order: { json: string; cbor: Uint8Array };
  mail: { encodeType: string; typeHash: string; hashStruct: string; digest: string };
  generate(models: string): string;
}

function hex(bytes: Uint8Array): string {
  return '0x' + [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function compute(): Promise<SiteValues> {
  // The site builds from site/, so the module's path is relative to it.
  const wasm = await readFile(join(process.cwd(), 'src/wasm/fe_codec_playground.wasm'));
  const { FeCodecPlayground } = await import('../wasm/fe_codec_playground.js');
  let words: bigint[] = [];
  const guest = await FeCodecPlayground.load(wasm, { onLog: (v: number | bigint) => words.push(BigInt(v)) });
  const pieces = (): Uint8Array[] => {
    const out: Uint8Array[] = [];
    let i = 0;
    while (i < words.length) {
      const length = Number(words[i++]);
      const bytes = new Uint8Array(length);
      for (let b = 0; b < length; b++) bytes[b] = Number((words[i + (b >> 3)] >> BigInt(8 * (b & 7))) & 0xffn);
      i += Math.ceil(length / 8);
      out.push(bytes);
    }
    words = [];
    return out;
  };

  const order = guest.encodeOrder(ORDER);
  if (!order.ok) throw new Error(`fe_codec_json rejected the sample Order (error ${order.code})`);
  const [json, cbor] = pieces();

  const mail = guest.hashMail(MAIL);
  if (!mail.ok) throw new Error(`the sample Mail failed to hash (error ${mail.code})`);
  const [generated, generic, digest, , encodeType, typeHash, genericTypeHash] = pieces();
  if (hex(generated) !== hex(generic) || hex(typeHash) !== hex(genericTypeHash)) {
    throw new Error('the generated and generic EIP-712 paths disagree on the sample Mail');
  }

  return {
    order: { json: decoder.decode(json), cbor },
    mail: {
      encodeType: decoder.decode(encodeType),
      typeHash: hex(typeHash),
      hashStruct: hex(generated),
      digest: hex(digest),
    },
    generate(models: string): string {
      const reply = guest.generateModule(models, false);
      const [text] = pieces();
      if (!reply.ok || reply.value !== 0) throw new Error(`fe_codec_gen rejected a sample: ${decoder.decode(text)}`);
      return decoder.decode(text);
    },
  };
}

let values: Promise<SiteValues> | undefined;

/** The values, computed once per build. */
export function siteValues(): Promise<SiteValues> {
  values ??= compute();
  return values;
}

/** Fails the build when `text` does not contain `expected`, which Fe Codec produced. */
export function expectIn(where: string, text: string, expected: string, what: string): void {
  if (!text.includes(expected)) {
    throw new Error(`${where} no longer shows ${what} as Fe Codec produces it; expected:\n${expected}`);
  }
}

/** Pages that quote bytes or hashes must quote what Fe Codec produces. */
export async function checkQuotedValues(entries: { id: string; body?: string }[]) {
  const { order, mail } = await siteValues();
  const body = (id: string) => entries.find((entry) => entry.id === id)?.body ?? '';
  const cbor = [...order.cbor].map((b) => b.toString(16).padStart(2, '0')).join('');
  const quotedCbor = body('getting-started')
    .split('\n')
    .filter((line) => /^[0-9a-f ]{40,}$/.test(line))
    .map((line) => line.replace(/ /g, ''));
  expectIn('docs/getting-started.md', body('getting-started'), order.json, 'the Order in JSON');
  expectIn('docs/getting-started.md', quotedCbor.join('\n'), cbor, 'the Order in CBOR');
  expectIn('docs/eip712.md', body('eip712'), mail.encodeType, "Mail's encodeType");
  expectIn('docs/eip712.md', body('eip712'), mail.hashStruct, "the mail's hashStruct");
  expectIn('docs/eip712.md', body('eip712'), mail.digest, "the mail's signing digest");
}
