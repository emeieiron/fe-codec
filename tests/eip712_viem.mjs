// Checks the EIP-712 vectors in tests/vectors/eip712_cases.json with viem, an
// implementation independent of both Fe Codec and tests/vectors/eip712.py.
//
// Each case's `json` is byte-for-byte what fe_codec_eip712::typed_data_json writes (the Fe
// tests assert that). Hashing it here with viem checks that the request a wallet signs has
// the digest a contract verifies.
//
// Usage: node tests/eip712_viem.mjs --viem <path to a viem package directory>

import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import path from "node:path";

const index = process.argv.indexOf("--viem");
if (index < 0 || !process.argv[index + 1]) {
  console.error("usage: node tests/eip712_viem.mjs --viem <path to viem>");
  process.exit(2);
}
const viemDir = path.resolve(process.argv[index + 1]);
const viem = await import(pathToFileURL(path.join(viemDir, "_esm/index.js")).href);
// encodeType is not re-exported from viem's index.
const { encodeType } = await import(
  pathToFileURL(path.join(viemDir, "_esm/utils/signature/hashTypedData.js")).href
);
const { version } = JSON.parse(await readFile(path.join(viemDir, "package.json"), "utf8"));

const cases = JSON.parse(
  await readFile(new URL("./vectors/eip712_cases.json", import.meta.url), "utf8"),
);

// Integers arrive as decimal strings; viem takes bigints for integer types.
function integers(type, value, types) {
  if (type.endsWith("]")) {
    const element = type.slice(0, type.lastIndexOf("["));
    return value.map((item) => integers(element, item, types));
  }
  if (types[type]) {
    return Object.fromEntries(
      types[type].map((member) => [member.name, integers(member.type, value[member.name], types)]),
    );
  }
  return /^u?int\d+$/.test(type) ? BigInt(value) : value;
}

let failures = 0;
for (const testCase of cases) {
  const request = JSON.parse(testCase.json);
  const { EIP712Domain, ...types } = request.types;
  const domain = integers("EIP712Domain", request.domain, { EIP712Domain });
  const message = integers(request.primaryType, request.message, types);
  const typedData = { domain, types, primaryType: request.primaryType, message };
  const got = {
    encodeType: encodeType({ primaryType: request.primaryType, types }),
    hashStruct: viem.hashStruct({ data: message, primaryType: request.primaryType, types }),
    domainSeparator: viem.hashDomain({ domain, types: { EIP712Domain } }),
    digest: viem.hashTypedData(typedData),
  };
  for (const [key, value] of Object.entries(got)) {
    if (value !== testCase[key]) {
      failures += 1;
      console.error(`${testCase.label} ${key}: viem ${value}, expected ${testCase[key]}`);
    }
  }
}
if (failures) {
  process.exit(1);
}
console.log(`viem ${version} agrees on ${cases.length} EIP-712 cases`);
